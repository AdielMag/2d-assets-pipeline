"""Measurement harness for the screen pipeline's Text and Polish steps.

Both steps hand work to an image model through a single function
(`routers.mockups._llm_reference_pass`), and until now the model, its per-model params and
the prompt wording were all fixed at whatever the default happened to be. This package runs
the same work across several of each and grades the results, so those three choices can be
made from evidence.

The hard rule here is that **nothing in this package writes to the application database**.
Polish and Text normally create AssetVersion rows, reassign `asset.selected_version_id`,
raise `asset.resolution` and delete superseded text regions; a sweep doing that would
rewrite the catalogue it is supposed to be measuring against. So the runner reads what it
needs, closes the session, and writes every output under `storage/experiments/<run_id>/`.
"""
