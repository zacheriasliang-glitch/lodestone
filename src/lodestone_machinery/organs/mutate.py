"""MUTATE changes one typed locus into a new immutable candidate profile."""
import copy
from ._kernel import fields, require, text, digest, typed_equal, _json_copy


def mutate(store, parent_handle, mutagen):
    parent = store.read(parent_handle)
    require((parent['owner'], parent['kind']) in (('eat', 'kernel_blueprint'), ('mutate', 'kernel_variant')),
            'MUTATE requires an EAT blueprint or prior variant')
    store.verify_links([parent_handle])
    profile = _json_copy(parent['payload'].get('profile'))
    fields(profile, 'schema id kind source_refs operators instructions lineage', 'parent profile')
    require(type(profile) is dict and profile.get('schema') == 'kernel-profile/v1'
            and type(profile.get('operators')) is dict and 1 <= len(profile['operators']) <= 32
            and type(profile['lineage']) is dict and type(profile['instructions']) is list
            and type(profile['source_refs']) is list, 'Invalid parent profile')
    text(profile['id'], 'parent id', 96)
    mutagen = _json_copy(mutagen)
    fields(mutagen, 'schema id new_id operator expected replacement rationale', 'mutagen')
    require(mutagen['schema'] == 'kernel-mutagen/v1', 'Unsupported mutagen schema')
    for key in ('id', 'new_id', 'operator'): text(mutagen[key], key, 96)
    text(mutagen['rationale'], 'rationale')
    require(mutagen['new_id'] != profile['id'], 'Mutation needs new child id')
    locus = mutagen['operator']
    require(locus in profile['operators'], 'Mutation locus not present')
    require(typed_equal(profile['operators'][locus], mutagen['expected']), 'Typed expected value mismatch')
    require(not typed_equal(mutagen['expected'], mutagen['replacement']), 'Mutation must change the locus')
    child = copy.deepcopy(profile)
    child['id'], child['kind'] = mutagen['new_id'], 'inferred-candidate'
    child['operators'][locus] = mutagen['replacement']
    child['lineage'] = {'operation': 'MUTATE', 'parent_id': profile['id'], 'parent_record': parent_handle,
                        'parent_profile_sha256': digest(profile), 'parent_lineage': profile['lineage'],
                        'mutagen_sha256': digest(mutagen), 'operator': locus,
                        'before': mutagen['expected'], 'after': mutagen['replacement']}
    child = _json_copy(child)  # Output must satisfy the next mutation's input bounds.
    return store.publish('mutate', 'kernel_variant',
                         {'schema': 'kernel-variant/v1', 'profile': child, 'mutagen': mutagen,
                          'status': 'CANDIDATE', 'fitness': 'UNMEASURED', 'activation': 'NONE',
                          'source_instructions_executed': False, 'identity_transfer': False},
                         links=[parent_handle])
