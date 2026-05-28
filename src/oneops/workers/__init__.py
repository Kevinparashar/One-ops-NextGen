"""Worker processes — long-running consumers of NATS queue subjects.

Each worker subscribes to one subject (`oneops.request.chat` for the graph
worker today; future workers handle scheduled tasks, embeddings, etc.).
Workers scale horizontally via NATS queue groups — multiple replicas
share the same subject, each message goes to exactly one replica.

In the demo, the graph worker is embedded in the same uvicorn process as
the ingress (one-process FaaS-style). In production, the worker is its
own deployable, run via `python -m oneops.workers.graph_worker` — same
code, different process boundary.
"""

from oneops.workers.graph_worker import GraphWorker, build_graph_worker

__all__ = ["GraphWorker", "build_graph_worker"]
