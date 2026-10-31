# Building domain

This package turns architectural evidence into a shared Sim-BIM model. The user entry is `sim_bim.py`; model transport and execution infrastructure live in `src/agent_runtime`.

## Responsibilities

| Package or module | Responsibility |
|---|---|
| `geometry_model` | Typed geometry inputs: spaces, storeys, boundaries, openings and evidence |
| `geometry` | Compile, inspect and edit geometry; calculate relationships; project views |
| `execution` | Save candidates, reports and source provenance |
| `runtime_roles` | Drawing coordinator, plan reader, elevation reader and assembly |
| `runtime_entry.py` | Single-model workflow with optional mesh observers |
| `runtime_configuration.py` | User configuration, input routing and process launch |
| `bim_inputs.py` | Freeze explicitly supplied inputs and record their identity |
| `sdk.py` | Development API over the same tools and delivery rules |

Model-callable tools and their guidance live in `scripts/tool_scripts`. They share the same geometry and source output in both workflows.

## Inputs and outputs

The drawing workflow coordinates PNG plan and elevation readings. The mesh workflow uses a textured GLB and may also receive PNG references and a JSON building declaration. Declared facts, observations and assumptions retain their separate provenance.

Both workflows deliver `source_model.json` and an HTML viewer. The model contains physical spaces, boundaries, openings, contacts, connectivity and declared uncertainty. Geometry checks establish internal consistency; they do not establish fidelity to the original evidence. A completed run and architectural quality are reported separately.

## Geometry contracts

`geometry_model` owns input types and parsing; `geometry` owns algorithms. The rectangular/orthogonal input contract and `source_polygon_v1` are both current inputs. Polygon input supports declared holes and stepped vertical sections where the corresponding algorithms allow them. A geometric feature is validated explicitly instead of silently flattened or inferred.

Local edits retain their audit trail in `corrections`. This field records changes to model geometry. Shared parameter plans can expand repeated floors while keeping source identities, opening hosts and connections explicit.

See the [architecture](../../../AI_agent/design/architecture.md) for runtime boundaries and the [SDK guide](SDK.md) for development integration.
