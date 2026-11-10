"""Conservative, source-grounded identities; fuzzy matches are only candidates."""
import re
from difflib import SequenceMatcher

from .contracts import VERSION, normalize_alias, stable_id
from .evidence import has_direct_name, plain_source

ENTITY_TYPES = {'person', 'project', 'pet', 'organization', 'account', 'asset',
                'task', 'place', 'other'}
SENSITIVE_TYPES = {'person', 'account', 'pet'}
PRONOUNS = {'он', 'она', 'они', 'оно', 'его', 'её', 'ее', 'их', 'я', 'мы', 'ты',
            'вы', 'he', 'she', 'they', 'it', 'him', 'her', 'them', 'i', 'we', 'you'}
IDENTITY_MARKER = re.compile(r'извест\w*\s+как|\bник(?:ом|нейм)?\b|псевдоним|\bэто\b|'
                             r'also known as|\baka\b|same person|same project', re.I)
DISTINCT_MARKER = re.compile(r'другой человек|другая персона|другой Алексей|different person|another person', re.I)
UNSAFE_IDENTITY = re.compile(r'\bне\b|\bnot\b|никогда|разные люди|разные лица|different people|'
                             r'возможно|может быть|говорят|по словам|планиру\w*|\bmaybe\b|'
                             r'\bmight\b|\breportedly\b|\bplans?\b|\?', re.I)


def scoped_handle(value, scope, quote):
    if not value.startswith('@'):
        return True
    if not re.fullmatch(r'service:[a-z0-9][a-z0-9_.-]{0,63}', scope):
        return False
    return scope.split(':', 1)[1] in normalize_alias(plain_source(quote))


def explicit_identity(quote, name, alias, scope):
    return (has_direct_name(quote, name) and has_direct_name(quote, alias)
            and not UNSAFE_IDENTITY.search(plain_source(quote))
            and not DISTINCT_MARKER.search(plain_source(quote))
            and bool(IDENTITY_MARKER.search(plain_source(quote)))
            and scoped_handle(alias, scope, quote))


def resolution(status, candidates=(), chosen=None, basis=()):
    return {'version': VERSION, 'status': status, 'candidate_entity_ids': sorted(set(candidates)),
            'chosen_entity_id': chosen, 'basis': list(basis)}


def resolve_mention(mention, ev, hint_ev, document, ledger):
    name = normalize_alias(mention['canonical_name'])
    surface = mention['surface']
    entities, aliases = ledger.entities(), ledger.aliases()
    by_id = {e['id']: e for e in entities}
    kind = mention['type'] if mention['type'] in ENTITY_TYPES else 'other'
    if normalize_alias(surface) in PRONOUNS:
        return resolution('unresolved', basis=['pronoun_only']), None
    if ev['source_role'] != 'direct':
        return resolution('unresolved', basis=['non_direct_identity_evidence']), None
    if not has_direct_name(ev['quote'], surface):
        return resolution('unresolved', basis=['link_only_identity']), None
    if not has_direct_name(ev['quote'], mention['canonical_name']):
        return resolution('unresolved', basis=['canonical_name_not_in_mention_anchor']), None
    if ev['origin'] == 'title':
        return resolution('unresolved', basis=['title_only_identity']), None
    if surface.startswith('@') and not scoped_handle(surface, mention['scope'], ev['quote']):
        return resolution('unresolved', basis=['unscoped_handle']), None
    anchored = {e['id'] for e in entities if ev['id'] in e.get('evidence_ids', [])
                and normalize_alias(e['canonical_name']) == name and e['type'] == kind}
    if len(anchored) == 1:
        chosen = next(iter(anchored))
        return resolution('resolved', anchored, chosen, ['exact_grounded_mention']), None
    if len(anchored) > 1:
        return resolution('ambiguous', anchored, basis=['grounded_mention_collision']), None
    distinct = mention['distinct'] or bool(DISTINCT_MARKER.search(ev['quote']))
    hint = mention['identity_hint']
    if hint and not distinct:
        if (hint_ev['source_role'] != 'direct' or not explicit_identity(
                hint_ev['quote'], surface, hint['value'], hint['scope'])):
            return resolution('unresolved', basis=['ungrounded_identity_hint']), None
        ids = {a['entity_id'] for a in aliases if a['status'] == 'approved'
               and a['normalized_value'] == normalize_alias(hint['value']) and a['scope'] == hint['scope']}
        collisions = {a['entity_id'] for a in aliases if a['entity_id'] is not None
                      and a['normalized_value'] == normalize_alias(hint['value']) and a['scope'] == hint['scope']}
        if len(collisions) > 1:
            return resolution('ambiguous', collisions, basis=['alias_collision']), None
        if len(ids) == 1:
            chosen = next(iter(ids))
            if by_id[chosen]['type'] != kind:
                return resolution('unresolved', ids, basis=['identity_type_conflict']), None
            return resolution('resolved', ids, chosen, ['approved_scoped_alias', 'direct_identity_context']), None
        # An unregistered hint cannot silently substitute for a canonical ID.
    if not distinct:
        exact_names = {e['id'] for e in entities if normalize_alias(e['canonical_name']) == name}
        exact_aliases = {a['entity_id'] for a in aliases if a['status'] == 'approved'
                         and a['scope'] == mention['scope']
                         and a['normalized_value'] == normalize_alias(surface)}
        alias_candidates = {a['entity_id'] for a in aliases if a['entity_id'] is not None
                            and a['scope'] == mention['scope']
                            and a['normalized_value'] == normalize_alias(surface)}
        candidates = exact_names | (alias_candidates if exact_aliases else set())
        if len(candidates) > 1:
            return resolution('ambiguous', candidates, basis=['name_or_alias_collision']), None
        if candidates:
            chosen = next(iter(candidates))
            entity = by_id[chosen]
            if entity['type'] != kind:
                return resolution('unresolved', candidates, basis=['identity_type_conflict']), None
            same_document = entity.get('origin_document_id') == document['document_id']
            alias_context = (chosen in exact_aliases and has_direct_name(ev['quote'], entity['canonical_name'])
                             and explicit_identity(ev['quote'], entity['canonical_name'], surface, mention['scope']))
            if kind in SENSITIVE_TYPES and not alias_context:
                return resolution('unresolved', candidates, basis=['name_alone_not_identity']), None
            return resolution('resolved', candidates, chosen,
                              ['approved_alias' if chosen in exact_aliases else 'exact_canonical_name',
                               'same_document_context' if same_document else 'direct_context']), None
        fuzzy = sorted(((SequenceMatcher(None, name, normalize_alias(e['canonical_name'])).ratio(), e['id'])
                        for e in entities if e['type'] == kind), key=lambda p: (-p[0], p[1]))
        shortlist = [entity_id for score, entity_id in fuzzy[:5] if score >= 0.86]
        if shortlist:
            return resolution('unresolved', shortlist, basis=['fuzzy_candidate_requires_review']), None
    entity_id = stable_id('ent', ledger.vault_id, document['document_id'], ev['revision_id'],
                          ev['start_byte'], name, kind)
    entity = {'version': VERSION, 'id': entity_id, 'vault_id': ledger.vault_id,
              'type': kind, 'type_label': mention['type'] if kind == 'other' else None,
              'canonical_name': mention['canonical_name'], 'state': 'staged',
              'origin_document_id': document['document_id'],
              'evidence_ids': [ev['id']]}
    return resolution('new_candidate', [entity_id], entity_id,
                      ['distinct_source_identity' if distinct else 'new_grounded_identity']), entity


def audit_alias(alias, target, mention, ev, ledger):
    chosen = target['chosen_entity_id']
    reasons = []
    if target['status'] not in {'resolved', 'new_candidate'}:
        reasons.append('unresolved_alias_target')
    if ev['source_role'] != 'direct' or not explicit_identity(
            ev['quote'], mention['surface'], alias['value'], alias['scope']):
        reasons.append('alias_not_directly_supported')
    if alias['value'].startswith('@') and not scoped_handle(alias['value'], alias['scope'], ev['quote']):
        reasons.append('unscoped_handle')
    normalized = normalize_alias(alias['value'])
    collisions = {a['entity_id'] for a in ledger.aliases()
                  if a['normalized_value'] == normalized and a['scope'] == alias['scope']
                  and a['entity_id'] != chosen}
    if alias['scope'] == 'local':
        collisions |= {e['id'] for e in ledger.entities()
                       if normalize_alias(e['canonical_name']) == normalized and e['id'] != chosen}
    if collisions:
        reasons.append('alias_collision')
    identity_parts = [chosen, alias['value'], alias['scope']]
    if chosen is None:
        identity_parts.append(ev['id'])
    value = {'version': VERSION, 'id': stable_id('als', *identity_parts),
             'entity_id': chosen, 'value': alias['value'], 'normalized_value': normalized,
             'kind': alias['kind'], 'scope': alias['scope'],
             'status': 'proposed' if reasons else 'approved', 'state': 'staged',
             'evidence_ids': [ev['id']], 'candidate_entity_ids': sorted(collisions)}
    return value, reasons
