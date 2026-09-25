"""Look at the real picture, from the real cluster, at a real panel size.

Not a test. Run it to see what the TUI is about to show:

    uv run python tests/preview_graph.py [width] [height]
"""

from __future__ import annotations

import sys

from kubby.service import KubbyService
from kubby.tui import diagram, paint, place


def main() -> int:
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    height = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    service = KubbyService()
    info = service.get_cluster_info()
    cluster = service.get_cluster_graph(info)
    print(f"context={info.get('context')} version={info.get('version')} "
          f"graph available={cluster.get('available')}")
    # The panel is the body minus the sidebar, so the picture gets roughly
    # this many columns on a terminal of the given width.
    panel = max(20, width - 36)

    built = diagram.build_diagram(cluster)
    placement = place.place(built, panel)
    print(f"{len(built.nodes)} boxes, {len(built.edges)} edges, "
          f"groups={[h for _x, _y, h in placement.headings]}")
    print(f"asked for {panel} columns, picture is {placement.width} wide "
          f"and {placement.height} tall (terminal height {height})")
    print("-" * panel)
    print(paint.render(placement).plain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
