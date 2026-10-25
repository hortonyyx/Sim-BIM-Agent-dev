# Python BIM tools

Use ordinary Python to prepare shared coordinates, loops and templates, then call
the existing domain tools. This interface starts no model and supplies no new
geometry rules. It uses the same input admission, build checks and finish contract
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
