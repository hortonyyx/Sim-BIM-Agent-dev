"""Preserve declared junction incidence while adopting supporting-line offsets.

This is a linear construction from existing coincident corners, not a proximity
repair. Independent reference lines retain their adopted grid offsets; dependent
lines pass through the same constructed intersection. Directions never change.
"""
from __future__ import annotations

import math


def adopt_shared_incidence(records, templates, frames, audit, *, epsilon):
    """Constrain offsets only where source corners already share an identity.

    Templates and distinct absolute-Z sections have separate incidence scopes.
    Supporting-line families still share the caller's existing frame anchors.
    Redundant collinear vertices do not acquire a new corner identity.
    """
    scopes = {}
    active_names = {record[0] for record in records}
    for name, template in templates.items():
        if name not in active_names:
            continue
        sectioned = any(space.get('vertical_sections') for space in template['spaces'])
        footprint = template['footprint']
        rings = [footprint] if isinstance(footprint, list) else [
            footprint['vertices'], *footprint.get('holes', [])]
        if not sectioned:
            for ring in rings:
                scopes[id(ring)] = (name, 'projection')
        for space in template['spaces']:
            if not sectioned:
                for ring in [space['polygon'], *space.get('holes', [])]:
                    scopes[id(ring)] = (name, 'projection')
            for section in space.get('vertical_sections', []):
                for ring in [section['polygon'], *section.get('holes', [])]:
                    scopes[id(ring)] = (name, 'section', *section['z'])

    variables, nodes, bins = {}, [], {}
    for name, path, ring, points, edges in records:
        scope = scopes.get(id(ring))
        if scope is None:
            continue
        for index, point in enumerate(points):
            previous, current = edges[index-1], edges[index]
            keys = []
            for family, offset in (previous, current):
                representative = family['representatives'][offset]
                key = (id(family), representative)
                if key not in variables:
                    n = family['normal']
                    members = [d for d, rep in family['representatives'].items()
                               if rep == representative]
                    endpoints = [p for d, a, b in family['endpoints'] if d in members for p in (a, b)]
                    extent = max(sum(v*w for v, w in zip(family['tangent'], p)) for p in endpoints) - min(
                        sum(v*w for v, w in zip(family['tangent'], p)) for p in endpoints)
                    variables[key] = dict(family=family, members=members,
                        target=family['mapping'][offset], normal=n,
                        # Keep cardinal and long reference supports independent
                        # before less informative dependent supports.
                        priority=(min(abs(n[0]), abs(n[1])) > 1e-10, -extent,
                                  round(n[0], 12), round(n[1], 12), representative),
                        frame_id=frames[name]['id'])
                keys.append(key)
            cell = tuple(math.floor(v / epsilon) for v in point)
            candidates = [node for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                          for node in bins.get((*scope, cell[0]+dx, cell[1]+dy), [])]
            node = next((node for node in candidates
                         if all(math.dist(point, p) <= epsilon for p in node['points'])), None)
            if node is None:
                node = dict(scope=scope, points=[], lines=set(), paths=set())
                nodes.append(node)
                bins.setdefault((*scope, *cell), []).append(node)
            node['points'].append(point)
            node['lines'].update(keys)
            node['paths'].add(path)

    ordered = sorted(variables, key=lambda key: (variables[key]['frame_id'], variables[key]['priority']))
    rank = {key: index for index, key in enumerate(ordered)}
    basis, constrained_nodes = {}, []
    for node in sorted(nodes, key=lambda n: (n['scope'], n['points'][0])):
        # Two supports already have a unique intersection. Only a source node
        # with at least three distinct supports adds an incidence constraint.
        keys = sorted(node['lines'], key=rank.get)
        if len(keys) < 3 or len(node['paths']) < 2:
            continue
        pairs = [(a, b) for i, a in enumerate(keys) for b in keys[i+1:]]
        def determinant(pair):
            a, b = (variables[key]['normal'] for key in pair)
            return a[0]*b[1]-a[1]*b[0]
        first, second = max(pairs, key=lambda pair: abs(determinant(pair)))
        a, b = variables[first]['normal'], variables[second]['normal']
        det = determinant((first, second))
        if abs(det) <= 1e-10:
            continue
        added = 0
        for key in keys:
            if key in (first, second):
                continue
            c = variables[key]['normal']
            row = {key: 1., first: -(c[0]*b[1]-c[1]*b[0])/det,
                   second: -(a[0]*c[1]-a[1]*c[0])/det}
            # Homogeneous equations constrain only adopted offsets; no source
            # position is fitted and no approximate node is welded.
            for pivot in sorted(basis, key=rank.get, reverse=True):
                factor = row.get(pivot, 0.)
                if factor:
                    for column, value in basis[pivot].items():
                        row[column] = row.get(column, 0.) - factor*value
            scale = max(map(abs, row.values()), default=0.)
            row = {column: value for column, value in row.items()
                   if abs(value) > max(1e-12, scale*1e-10)}
            if not row:
                continue
            # A preference for a reference direction must not divide by a
            # tiny coefficient at a nearly collinear junction. Pivot among
            # well-conditioned coefficients before applying anchor priority.
            largest = max(map(abs, row.values()))
            pivot = max((key for key, value in row.items() if abs(value) >= .1*largest), key=rank.get)
            denominator = row[pivot]
            row = {column: value/denominator for column, value in row.items()}
            for existing in basis.values():
                factor = existing.get(pivot, 0.)
                if factor:
                    for column, value in row.items():
                        existing[column] = existing.get(column, 0.) - factor*value
                    existing.pop(pivot, None)
            basis[pivot] = row
            added += 1
        if added:
            constrained_nodes.append(dict(template=node['scope'][0], scope=list(node['scope'][1:]),
                source_local_point=node['points'][0], support_count=len(keys), constraint_count=added))

    if not basis:
        return
    adopted = {key: value['target'] for key, value in variables.items()}
    for pivot, row in basis.items():
        adopted[pivot] = -sum(value*adopted[key] for key, value in row.items() if key != pivot)
    changes = []
    for key, value in variables.items():
        target = adopted[key]
        if not math.isfinite(target):
            raise ValueError('shared-incidence construction produced a nonfinite support')
        if abs(target-value['target']) <= 1e-12:
            continue
        for offset in value['members']:
            value['family']['mapping'][offset] = target
        changes.append(dict(frame_id=value['frame_id'], normal=value['normal'],
            observed_offsets_m=sorted(value['members']), independent_grid_offset_m=value['target'],
            adopted_offset_m=target))
    audit['changes'].append(dict(type='shared_incidence_anchors',
        basis='exact source junctions constrain dependent supports before ring construction',
        node_tolerance_m=epsilon, nodes=constrained_nodes, derived_supports=changes,
        independent_grid_support_count=len(variables)-len(basis)))
