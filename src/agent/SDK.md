# Python BIM tools

Use ordinary Python to prepare shared coordinates, loops and templates, then call
the existing domain tools. This interface starts no model. It uses the same
input admission, build checks and finish contract
as the MCP server. The caller's runtime owns filesystem permissions and budgets;
this library is not a Python sandbox. Use one writer per run.

```python
from src.agent.sdk import BimSession, edge_segment

with BimSession(run_directory) as bim:
    print(bim.catalog())                    # names and short descriptions
    print(bim.describe("view_mesh"))        # exact current argument schema
    print(bim.call("inputs"))
    result = bim.call("view_mesh", azimuth_degrees=0, elevation_degrees=0)
    print(result)                          # metadata and absolute image paths
```

Actually open returned image paths with the agent's image viewer. Creating a view
does not mean it has been inspected. Complete tool receipts and lossless image
bytes are retained in `run_directory/sdk/`.

For construction, call `get_bim_reference(topic="parametric")` once for the current
format. Pass normal Python dictionaries/lists to parameters ending in `_json`;
the SDK encodes them. Read other references only when relevant. Existing
`primitive_frames`, rectangle unions and `local_baseline` express local frames;
ordinary loops can express repeated connections or floor exceptions.

```python
# Use endpoints from the same polygon, not separately rounded copies.
door = dict(id="access", space="hall", other_space="room", z=[0, 2.1],
            source_refs=["input plan: declared shared edge"],
            **edge_segment(edge_start, edge_end, offset=0.4, width=0.9))
# Construct plan with ordinary Python data, variables, loops and deepcopy.
with BimSession(run_directory) as bim:
    saved = bim.call("build_parametric_bim", plan_json=plan,
                     mesh_observations=observations)
    print(saved)  # includes current checks, saved plans and selected overlays
```

`reply.data` is the tool's structured result; `reply.texts` contains additional
messages; `reply.images` contains image paths, hashes and MIME types.
Printing a reply (including the CLI) shows a compact presentation: repeated input
declarations, implementation inventories and regularization change logs have
file or JSON pointers instead of full copies. Errors, warnings, rejection
evidence and current domain checks remain visible. `reply.data` and the receipt
at `reply.receipt_file` retain the complete returned result; image bytes are
unchanged. `reply.presentation()` returns the same display object as JSON text.
Remaining execution allowance is a hard run limit, not a refinement budget:
the task's delivery target may be shorter and takes precedence.
`reply.require_ok()` raises on tool errors only. A saved source, clear machine
check, or returned image does not certify architectural quality. Inspect the saved
result against the inputs, combine concrete necessary repairs, and recheck affected
relationships. Do not continue optional refinement merely to remove uncertainty
notes. Preserve useful detail already understood; infer necessary functions and
circulation without claiming unobserved details as fact.

Use normal `finish_bim` after the intended edits and actual architectural review.
For mesh, supply its current `mesh_review` with inspected overlay IDs. No wrapper
automatically retries writes, approves views, repairs geometry, calls finish or
spawns a worker. Failed drafts remain available through the normal patch tools.

## Construct shared regions and explicit openings

`src.agent.modeling` supplies four ordinary Python functions. They work in the
same metre-based coordinate frame; they do not read images or decide architecture.

```python
from shapely.geometry import box
from src.agent.modeling import region, shared_edges, shared_opening, clearance_report

hall = region(box(0, 0, 7, 2))
room = region(box(0, 2, 4, 6))
edges = shared_edges(hall, room)  # inspect all actual shared straight segments
door = shared_opening(hall, room, edge=0, offset=1.2, width=0.9)
# Merge **hall / **room into normal parametric space records, and **door into a
# door record with explicit id, space, other_space, z and source_refs.
print(clearance_report(hall, width=0.9))
```

`region(*parts, subtract=...)` returns existing polygon/holes fields. Empty or
disconnected results and touching holes are rejected; no small part is discarded.
`shared_opening` preserves the requested width or reports a conflict. Never shrink
it with `min(width, available_length)`. Reconsider the space/entrance organization
if the intended opening cannot fit. `clearance_report` is a circular passage-body
probe at **your supplied width**, including approximation limits; it cannot certify
entry-to-exit access, regulations or building quality.

Construct shared regions/openings in a common local frame before applying the
same transform. Independently transformed/subdivided rings can acquire tiny
floating-point gaps; these helpers do not weld, snap or search for a nearby wall.
Declare the existing `modelling.frames` when architectural reference-line adoption
uses local axes. Preserve the actual relationship to source images/mesh.

## Optional construction check and one-call delivery

Inside an ordinary Python script, `bim.check_plan(plan)` compiles with the current
run's adoption and source rules without allocating a candidate, exporting a
viewer or rendering plan images. It returns `.source`, `.report`, `.geometry_ready`
and `.receipt_file`; printing it shows findings and a complete receipt pointer.
Use it when testing construction alternatives or measuring the resulting source.
It is **optional**: checking and then saving performs compilation twice, so do not
insert it as a compulsory step for every save. A successful check is not a review
or a promise that a later save meets current budgets and delivery requirements.

Once the intended candidate has been inspected and required fixes are complete:

```python
published = bim.finish_to(new_output_directory, candidate, reviews=final_reviews)
print(published)
```

This calls the normal `finish_bim` and verifies its exact completed handoff, then
publishes the same source/viewer bytes to a **new** directory outside the native
run. It does not review, repair or render again. The directory contains
`source_model.json`, `delivery.html`, `publication.json` and the native viewer's
supporting files. Incomplete/stale handoffs and existing output directories are
rejected. No additional model-authored copy, schema validator or hash script is
needed. Publication is not independent architectural acceptance.

For ordinary drawing inputs, bind each relevant source image to its actual floor
with a current overlay before reviewing it:

```python
candidate = bim.call("build_parametric_bim", plan_json=plan).require_ok()["candidate"]
overlay = bim.call("overlay_candidate", candidate=candidate, image=image_name,
                   floor_id=floor_id, x_anchors=x_anchors, y_anchors=y_anchors,
                   basis=calibration_basis)
print(overlay)  # open the returned image and compare it with the original
# Only after actual inspection; record unresolved points in issues/outcome.
published = bim.finish_to(new_output_directory, candidate, reviews=[
    {"kind": "plan", "image": image_name, "floor_id": floor_id,
     "outcome": "consistent", "summary": actual_plan_findings, "issues": []},
    {"kind": "building", "outcome": "consistent",
     "summary": actual_building_findings, "issues": []},
])
```

Anchors map original image pixels to world coordinates; this is not automatic
registration. Read `bim.describe("overlay_candidate")` for their exact format.
Review each required image/floor scope. Calibration alone does not establish
coverage. Use `uncertain` with concrete issues for unresolved points; a
`needs_revision` review cannot establish new completed coverage. Changed source
geometry, original images, calibration or overlay evidence invalidate affected
review evidence. Do not manufacture a review merely to finish.

Runs are prepared through `src.agent.bim_inputs.prepare_bim_inputs`, from only the
authorized original inputs; the SDK constructor never copies a previous result.
Admission and export time belong in end-to-end measurements.

The same interface is available without an import:

```text
python -m src.agent.sdk --run RUN catalog
python -m src.agent.sdk --run RUN describe build_parametric_bim
python -m src.agent.sdk --run RUN call build_parametric_bim --arguments args.json
```

`args.json` is a UTF-8 JSON object, not a shell-escaped JSON string. CLI exit code 1
denotes a tool error. A process failure after a write can leave a saved candidate;
read the receipt/state before retrying. This interface is an experimental access
path; faster whole-building delivery remains to be measured.
