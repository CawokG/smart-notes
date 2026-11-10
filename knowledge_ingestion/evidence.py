"""Exact UTF-8 snapshot anchoring; offsets are never accepted from a model."""
import hashlib
import re
from pathlib import Path

from .contracts import VERSION, ValidationError, stable_id
from .storage import contains_symlink


class AnchorError(ValidationError):
    pass


def read_snapshot(revision):
    path = Path(revision['snapshot'])
    if contains_symlink(path) or not path.is_file():
        raise ValueError('snapshot_missing_or_symlink')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != revision['sha256']:
        raise ValueError('snapshot_hash_mismatch')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('snapshot_invalid_utf8') from None
    return raw


def locate(anchor, raw, revision, chunks, path='$'):
    quote = anchor['quote'].encode('utf-8')
    start, end = 0, len(raw)
    if anchor['chunk_ordinal'] is not None:
        matching = [c for c in chunks if c['ordinal'] == anchor['chunk_ordinal']]
        if not matching:
            raise AnchorError('unknown_anchor_chunk', path)
        start, end = matching[0]['start_byte'], matching[0]['end_byte']
    positions, cursor = [], start
    while cursor < end:
        pos = raw.find(quote, cursor, end)
        if pos < 0:
            break
        positions.append(pos)
        cursor = pos + 1
    if not positions:
        raise AnchorError('missing_quote', path)
    index = anchor['occurrence_index']
    if index is None:
        if len(positions) != 1:
            raise AnchorError('ambiguous_quote', path)
        index = 0
    if index >= len(positions):
        raise AnchorError('invalid_quote_occurrence', path)
    start, end = positions[index], positions[index] + len(quote)
    if raw[start:end].decode('utf-8') != anchor['quote']:
        raise AnchorError('quote_bytes_mismatch', path)
    origin = anchor['origin']
    if origin == 'title':
        # A title must actually be a Markdown heading, not model metadata.
        line_start = raw.rfind(b'\n', 0, start) + 1
        line_end = raw.find(b'\n', end)
        line = raw[line_start:line_end if line_end >= 0 else len(raw)].decode('utf-8')
        if not re.match(r'^\ufeff?#{1,6}[ \t]+', line):
            raise AnchorError('invalid_title_anchor', path)
    # Recognize only the fence range, never parse YAML values or tags. Such
    # bytes remain body text, but cannot establish structured identity/facts.
    frontmatter = re.match(rb'^(?:\xef\xbb\xbf)?---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)', raw)
    role = ('metadata' if frontmatter and start < frontmatter.end()
            else anchor['source_role'])
    return {'version': VERSION,
            'id': stable_id('ev', revision['id'], start, end, anchor['quote']),
            'revision_id': revision['id'], 'start_byte': start, 'end_byte': end,
            'quote': anchor['quote'], 'origin': origin,
            'source_role': role,
            'captured_at': revision.get('captured_at'),
            'parser_version': revision['parser_version']}


def validate_anchors(proposal, raw, revision, chunks):
    """Return source spans by proposal path, without writing partial results."""
    result = {}
    for i, mention in enumerate(proposal['entity_mentions']):
        path = f'entity_mentions[{i}]'
        ev = locate(mention['anchor'], raw, revision, chunks, path)
        # A mention's range is the surface, while its evidence can be a sentence.
        surface = mention['surface'].encode('utf-8')
        quote = ev['quote'].encode('utf-8')
        occurrences, offset = [], 0
        while True:
            offset = quote.find(surface, offset)
            if offset < 0:
                break
            occurrences.append(offset)
            offset += 1
        if len(occurrences) != 1:
            raise AnchorError('missing_mention_surface' if not occurrences else 'ambiguous_mention_surface', path)
        result[path] = ev
        if mention['identity_hint']:
            result[path + '.identity_hint'] = locate(mention['identity_hint']['anchor'], raw, revision, chunks, path + '.identity_hint')
    for i, alias in enumerate(proposal['aliases']):
        path = f'aliases[{i}]'
        result[path] = locate(alias['anchor'], raw, revision, chunks, path)
    for i, claim in enumerate(proposal['claim_proposals']):
        for j, anchor in enumerate(claim['anchors']):
            path = f'claim_proposals[{i}].anchors[{j}]'
            result[path] = locate(anchor, raw, revision, chunks, path)
    return result


def plain_source(text):
    """Discard link syntax for identity/fact gates; never resolve its targets."""
    text = re.sub(r'!?\[\[[\s\S]*?\]\]', '', text)
    return re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', text)


def has_direct_name(quote, name):
    before = r'(?<![\w@])' if name.startswith('@') else r'(?<!\w)' if name[:1].isalnum() else ''
    after = r'(?!\w)' if name[-1:].isalnum() else ''
    return bool(re.search(before + re.escape(name) + after, plain_source(quote)))
