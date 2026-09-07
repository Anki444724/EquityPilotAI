"""Worker entry point: `python -m app.worker`.

Runs the job worker and the scheduler in a dedicated process, which is what a
production deployment should do. Keeping heavy background work out of the API
process means a fifteen-second PDF render cannot starve the event loop serving
everyone else's requests.

The worker it starts drains **both** queues: the generic `BackgroundJob` queue
and the `DocumentJob` queue that document ingestion — uploads, re-indexing and
the Blogger sync — enqueues into. A deployment that runs this process with
`WORKER_ENABLED=false` on the API side has no other consumer for either queue,
so a worker that knew about only one of them silently parked every document
in `queued`. See `Worker.run_pass`.

The in-process alternative (`WORKER_ENABLED=true`) exists for single-container
deployments where a second process is not worth the money. Both use the same
`Worker` and `Scheduler`, so behaviour does not change with deployment shape.
"""
from __future__ import annotations

from app.services.platform.jobs.worker import main

if __name__ == "__main__":
    main()
