"""Look at the real picture, from the real cluster, in a real panel size.

Not a test. Run it to see what the TUI is about to show:

    uv run python tests/preview_graph.py [width] [height]
"""

from __future__ import annotations

import sys

from kubby.service import KubbyService
from kubby.tui import graph as graph_mod


def main() -> int:
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    height = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    service = KubbyService()
    cluster = service.get_cluster_info()
    cluster = {**cluster, "graph": service.get_cluster_graph(cluster)}
    print(f"context={cluster.get('context')} version={cluster.get('version')}")
    print(f"graph available={cluster['graph'].get('available')} "
          f"error={cluster['graph'].get('error')}")

    source = graph_mod.build_mermaid(cluster["graph"])
    print("-" * 78)
    print(source)
    print("-" * 78)
    picture, direction = graph_mod.render_best(
        source, width - 36, height - 16
    )
    print(picture)
    print("-" * 78)
    print(f"direction={direction} width={graph_mod._max_width(picture)} "
          f"lines={len(picture.plain.splitlines())}")

    rects = graph_mod.node_rects(source)
    print(f"boxes: {len(rects)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
