"""Versioned application contracts; no native MuninnDB payloads.

The scanner's SHA-256 IDs are deliberately retained. Wall-clock metadata is
excluded from identity and canonical hashes.
"""
import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal

VERSION = '1.0'
RULES_VERSION = 'offline-1'
PROMPT_VERSION = 'evidence-first-1'
PARSER_VERSION = '1'
SCHEMA_VERSION = 4


class ValidationError(ValueError):
    def __init__(self, code, path='$'):
        self.code, self.path = code, path
        super().__init__(f'{code} at {path}')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def stable_id(prefix, *parts):
    # Do not change this encoding: scan v1 identities already use it.
    payload = json.dumps(parts, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return prefix + '_' + hashlib.sha256(payload).hexdigest()


def normalize_text(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def normalize_alias(value):
    return normalize_text(value).casefold()


PREDICATES = {'works_on', 'owns', 'member_of', 'located_at', 'has_status',
              'uses', 'alias_of', 'related_to', 'knows', 'date_of_birth'}
PREDICATE_SYNONYMS = {'work_on': 'works_on', 'working_on': 'works_on',
                      'works on': 'works_on', 'status': 'has_status',
                      'is_member_of': 'member_of', 'is_located_at': 'located_at'}


def canonical_predicate(value):
    value = normalize_alias(value)
    value = PREDICATE_SYNONYMS.get(value, value)
    if value in PREDICATES:
        return value
    slug = value.removeprefix('other:')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', slug):
        raise ValidationError('invalid_predicate')
    return 'other:' + slug


def normalize_json(value, depth=0):
    if depth > 6:
        raise ValidationError('qualifiers_too_deep')
    if isinstance(value, str):
        return normalize_text(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError('non_finite_number')
        return int(value) if value.is_integer() else value
    if isinstance(value, list):
        return [normalize_json(v, depth + 1) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError('non_string_key')
            key = normalize_text(key)
            if key in result:
                raise ValidationError('normalized_key_collision')
            result[key] = normalize_json(item, depth + 1)
        return dict(sorted(result.items()))
    raise ValidationError('invalid_json_value')


def canonical_date(value):
    try:
        if re.fullmatch(r'\d{4}', value):
            date(int(value), 1, 1)
            return value
        if re.fullmatch(r'\d{4}-\d{2}', value):
            date.fromisoformat(value + '-01')
            return value
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value).isoformat()
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('timezone required')
        return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        raise ValidationError('invalid_absolute_time') from None


def canonical_time(value):
    result = {'from': None, 'to': None, 'precision': value['precision'],
              'temporal_expression': normalize_text(value['temporal_expression'])
              if value['temporal_expression'] else None}
    for key in ('from', 'to'):
        if value[key] is not None:
            result[key] = canonical_date(value[key])
    precision = result['precision']
    if precision == 'unknown' and (result['from'] or result['to'] or result['temporal_expression']):
        raise ValidationError('inconsistent_unknown_time')
    if precision == 'unresolved':
        if not result['temporal_expression'] or result['from'] or result['to']:
            raise ValidationError('inconsistent_unresolved_time')
    elif precision != 'unknown':
        if result['from'] is None:
            raise ValidationError('time_start_required')
        pattern = {'year': r'\d{4}', 'month': r'\d{4}-\d{2}',
                   'day': r'\d{4}-\d{2}-\d{2}'}.get(precision)
        if pattern and any(result[k] is not None and not re.fullmatch(pattern, result[k]) for k in ('from', 'to')):
            raise ValidationError('time_precision_mismatch')
    if result['from'] and result['to'] and result['from'] > result['to']:
        raise ValidationError('reversed_time_interval')
    return result


def canonical_object(value):
    kind = value['kind']
    if kind == 'entity':
        return {'kind': kind, 'entity_id': value['entity_id']}
    result = {'kind': kind, 'value': value['value']}
    if kind == 'literal':
        result['value'] = normalize_text(value['value'])
    elif kind == 'date':
        result['value'] = canonical_date(value['value'])
    elif kind == 'number':
        sign, digits, exponent = Decimal(str(value['value'])).as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        result['value'] = (('-' if sign else '') + ''.join(map(str, digits)) + 'e' + str(exponent)
                           if any(digits) else '0')
        result['unit'] = normalize_alias(value.get('unit') or '') or None
    return result


def claim_fingerprint(vault_id, subject_id, predicate, obj, qualifiers,
                      polarity, modality, valid_time):
    return content_hash({'vault_id': vault_id, 'subject': subject_id,
                         'predicate': canonical_predicate(predicate),
                         'object': canonical_object(obj),
                         'qualifiers': normalize_json(qualifiers),
                         'polarity': polarity, 'modality': modality,
                         'valid_time': canonical_time(valid_time)})


def object_schema(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def string(max_length=256, **kwargs):
    return {'type': 'string', 'minLength': 1, 'maxLength': max_length, **kwargs}


ANCHOR_SCHEMA = object_schema({
    'quote': string(6000),
    'chunk_ordinal': {'type': ['integer', 'null'], 'minimum': 0},
    'occurrence_index': {'type': ['integer', 'null'], 'minimum': 0},
    'origin': {'enum': ['body', 'title']},
    'source_role': {'enum': ['direct', 'reported', 'metadata']},
})
TIME_SCHEMA = object_schema({
    'from': {'type': ['string', 'null'], 'maxLength': 40},
    'to': {'type': ['string', 'null'], 'maxLength': 40},
    'precision': {'enum': ['unknown', 'year', 'month', 'day', 'interval', 'unresolved']},
    'temporal_expression': {'type': ['string', 'null'], 'maxLength': 200},
})
HINT_SCHEMA = object_schema({'value': string(), 'scope': string(), 'anchor': ANCHOR_SCHEMA})
MENTION_SCHEMA = object_schema({
    'ref': string(80, pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$'),
    'surface': string(), 'canonical_name': string(),
    'type': string(48, pattern=r'^[a-z][a-z0-9_]*$'),
    'scope': string(100), 'anchor': ANCHOR_SCHEMA,
    'identity_hint': {'oneOf': [HINT_SCHEMA, {'type': 'null'}]},
    'distinct': {'type': 'boolean'},
})
OBJECT_SCHEMA = {'oneOf': [
    object_schema({'kind': {'const': 'entity'}, 'ref': string(80)}),
    object_schema({'kind': {'const': 'literal'}, 'value': string(1000)}),
    object_schema({'kind': {'const': 'date'}, 'value': string(40)}),
    object_schema({'kind': {'const': 'number'}, 'value': {'type': 'number'},
                   'unit': {'type': ['string', 'null'], 'maxLength': 100}}),
    object_schema({'kind': {'const': 'boolean'}, 'value': {'type': 'boolean'}}),
]}
CLAIM_SCHEMA = object_schema({
    'ref': string(80), 'subject_ref': {'type': ['string', 'null'], 'maxLength': 80},
    'predicate': string(64), 'object': OBJECT_SCHEMA,
    'qualifiers': {'type': 'object', 'maxProperties': 30},
    'polarity': {'enum': ['positive', 'negative']},
    'modality': {'enum': ['asserted', 'hypothetical', 'question', 'reported', 'planned']},
    'valid_time': TIME_SCHEMA,
    'reported_by_ref': {'type': ['string', 'null'], 'maxLength': 80},
    'anchors': {'type': 'array', 'items': ANCHOR_SCHEMA, 'minItems': 1, 'maxItems': 8},
    'confidence_raw': {'type': 'number', 'minimum': 0, 'maximum': 1},
})
EXTRACTION_SCHEMA = object_schema({
    'version': {'const': VERSION},
    'entity_mentions': {'type': 'array', 'items': MENTION_SCHEMA, 'maxItems': 80},
    'aliases': {'type': 'array', 'maxItems': 80, 'items': object_schema({
        'entity_ref': string(80), 'value': string(),
        'kind': {'enum': ['nickname', 'handle', 'name', 'other']},
        'scope': string(100), 'anchor': ANCHOR_SCHEMA})},
    'claim_proposals': {'type': 'array', 'items': CLAIM_SCHEMA, 'maxItems': 100},
    'document_context_digest': {'type': 'string', 'maxLength': 2000},
})
EXTRACTION_SCHEMA['$schema'] = 'https://json-schema.org/draft/2020-12/schema'


def validate_schema(value, schema, path='$'):
    """Validate the deliberately small JSON Schema vocabulary used above.

    This is not a general-purpose JSON Schema engine. Unknown keywords are not
    accepted from a provider: only our code constructs the schema.
    """
    if 'oneOf' in schema:
        matches = 0
        for option in schema['oneOf']:
            try:
                validate_schema(value, option, path)
                matches += 1
            except ValidationError:
                pass
        if matches != 1:
            raise ValidationError('invalid_variant', path)
        return
    types = schema.get('type')
    actual = ('null' if value is None else 'boolean' if type(value) is bool else
              'integer' if type(value) is int else 'number' if type(value) is float else
              'string' if isinstance(value, str) else 'array' if isinstance(value, list) else
              'object' if isinstance(value, dict) else 'invalid')
    if types:
        allowed = [types] if isinstance(types, str) else types
        if actual not in allowed and not (actual == 'integer' and 'number' in allowed):
            raise ValidationError('invalid_type', path)
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ValidationError('invalid_constant', path)
    if 'enum' in schema and value not in schema['enum']:
        raise ValidationError('invalid_enum', path)
    if actual == 'string':
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValidationError('invalid_unicode', path) from None
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 100000):
            raise ValidationError('invalid_string_length', path)
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise ValidationError('invalid_string_pattern', path)
    if actual in ('number', 'integer'):
        if (actual == 'number' and not math.isfinite(value)) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValidationError('invalid_number', path)
    if actual == 'object':
        if len(value) > schema.get('maxProperties', 1000):
            raise ValidationError('too_many_properties', path)
        props = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys():
            raise ValidationError('missing_property', path)
        if schema.get('additionalProperties') is False and value.keys() - props.keys():
            raise ValidationError('unexpected_property', path)
        for key, item in value.items():
            if key in props:
                validate_schema(item, props[key], path + '.' + key)
    if actual == 'array':
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 10000):
            raise ValidationError('invalid_array_length', path)
        for i, item in enumerate(value):
            validate_schema(item, schema.get('items', {}), f'{path}[{i}]')


def parse_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValidationError('duplicate_json_key')
            result[key] = item
        return result
    if isinstance(value, str):
        if len(value) > 1024 * 1024:
            raise ValidationError('response_too_large')
        try:
            return json.loads(value, object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValidationError('non_finite_number')))
        except ValidationError:
            raise
        except (ValueError, RecursionError):
            raise ValidationError('invalid_json') from None
    return value


def validate_extraction(value):
    value = parse_json(value)
    try:
        size = len(canonical_json(value).encode('utf-8'))
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        raise ValidationError('invalid_json_value') from None
    if size > 1024 * 1024:
        raise ValidationError('response_too_large')
    validate_schema(value, EXTRACTION_SCHEMA)
    refs = [m['ref'] for m in value['entity_mentions']]
    if len(set(refs)) != len(refs):
        raise ValidationError('duplicate_mention_ref')
    claims = [c['ref'] for c in value['claim_proposals']]
    if len(set(claims)) != len(claims):
        raise ValidationError('duplicate_claim_ref')
    for alias in value['aliases']:
        if alias['entity_ref'] not in refs:
            raise ValidationError('unknown_alias_reference')
    for claim in value['claim_proposals']:
        targets = [claim['subject_ref'], claim['reported_by_ref']]
        if claim['object']['kind'] == 'entity':
            targets.append(claim['object']['ref'])
        if any(ref is not None and ref not in refs for ref in targets):
            raise ValidationError('unknown_claim_reference')
        canonical_predicate(claim['predicate'])
        canonical_time(claim['valid_time'])
        normalize_json(claim['qualifiers'])
        if claim['object']['kind'] != 'entity':
            canonical_object(claim['object'])
    # Reject non-JSON Python values anywhere, including arbitrary qualifiers.
    try:
        canonical_json(value)
    except (TypeError, ValueError):
        raise ValidationError('invalid_json_value') from None
    return value
