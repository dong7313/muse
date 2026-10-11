"""Orthographic view renderer for standard engineering projections."""

from typing import List, Tuple, Optional
import math
from enum import Enum
from .base import BaseRenderer, Edge2D


class ViewDirection(Enum):
    """Standard orthographic view directions."""
    TOP = "top"       # XZ plane, looking from +Y
    FRONT = "front"   # XY plane, looking from +Z
    RIGHT = "right"   # YZ plane, looking from +X
    BOTTOM = "bottom" # XZ plane, looking from -Y
    BACK = "back"     # XY plane, looking from -Z
    LEFT = "left"     # YZ plane, looking from -X


class OrthographicRenderer(BaseRenderer):
    """Renders orthographic (parallel) projections of 3D models.

    Standard engineering views:
    - Top: X-Z plane projection
    - Front: X-Y plane projection
    - Right: Y-Z plane projection
    """

    def __init__(self, view_width: float, view_height: float,
                 scale: float = 1.0, view_direction: ViewDirection = ViewDirection.TOP):
        """Initialize orthographic renderer.

        Args:
            view_width: Width of view area
            view_height: Height of view area
            scale: Drawing scale
            view_direction: Direction of view
        """
        super().__init__(view_width, view_height, scale)
        self.view_direction = view_direction
        self._init_projection()

    def _init_projection(self):
        """Initialize projection based on view direction.

        In CadQuery (right-handed coordinate system):
        - X = horizontal (width)
        - Y = depth (into/out of screen)
        - Z = vertical (height, pointing up)

        Standard engineering views:
        - TOP (顶视图): Looking from +Y, shows X vs Y - top-down view
        - FRONT (前视图): Looking from +Z, shows X vs Z - front view
        - RIGHT (右视图): Looking from +X, shows Y vs Z - right side view
        """
        # Configure axis mapping and flip based on view direction
        if self.view_direction == ViewDirection.TOP:
            # Top view (顶视图): Looking from +Y, shows X vs Z (top-down)
            # This is a top-down view showing the X-Z plane
            self.u_axis = ('x', 1)   # X -> horizontal
            self.v_axis = ('z', -1)  # Z -> vertical (inverted for SVG), not Y
            self.depth_axis = 'y'    # Y is depth (into/out of screen)
        elif self.view_direction == ViewDirection.FRONT:
            # Front view (前视图): Looking from +Z, shows X vs Z (front view)
            # This is a front view showing the X-Z plane
            self.u_axis = ('x', 1)    # X -> horizontal
            self.v_axis = ('z', -1)   # Z -> vertical (inverted for SVG)
            self.depth_axis = 'y'     # Y is depth
        elif self.view_direction == ViewDirection.RIGHT:
            # Right view (右视图): Looking from +X, shows Y vs Z (right side view)
            # This is a right side view showing the Y-Z plane
            self.u_axis = ('y', 1)    # Y -> horizontal
            self.v_axis = ('z', -1)   # Z -> vertical
            self.depth_axis = 'x'     # X is depth
        elif self.view_direction == ViewDirection.BOTTOM:
            # Bottom view: Looking from -Y, shows X vs Y
            self.u_axis = ('x', 1)
            self.v_axis = ('y', 1)    # Y -> vertical (not inverted)
            self.depth_axis = 'z'
        elif self.view_direction == ViewDirection.BACK:
            # Back view: Looking from -Z, shows X vs Z
            self.u_axis = ('x', -1)
            self.v_axis = ('z', -1)
            self.depth_axis = 'y'
        elif self.view_direction == ViewDirection.LEFT:
            # Left view: Looking from -X, shows Y vs Z
            self.u_axis = ('y', -1)
            self.v_axis = ('z', -1)
            self.depth_axis = 'x'

    def project_point(self, x: float, y: float, z: float) -> Tuple[float, float]:
        """Project 3D point to orthographic 2D coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            z: Z coordinate

        Returns:
            (u, v) 2D orthographic coordinates
        """
        # Map based on view direction
        u = self._get_coord(x, y, z, self.u_axis)
        v = self._get_coord(x, y, z, self.v_axis)

        return (u, v)

    def _get_coord(self, x: float, y: float, z: float,
                   axis: Tuple[str, int]) -> float:
        """Get coordinate value from axis specification.

        Args:
            x: X coordinate
            y: Y coordinate
            z: Z coordinate
            axis: (axis_name, sign)

        Returns:
            Coordinate value with sign applied
        """
        axis_name, sign = axis
        if axis_name == 'x':
            return x * sign
        elif axis_name == 'y':
            return y * sign
        elif axis_name == 'z':
            return z * sign
        return 0

    def project_edge(self, p1: Tuple[float, float, float],
                     p2: Tuple[float, float, float]) -> Optional[Edge2D]:
        """Project 3D edge to orthographic view.

        Args:
            p1: Start point (x, y, z)
            p2: End point (x, y, z)

        Returns:
            Edge2D or None if degenerate
        """
        x1, y1, z1 = p1
        x2, y2, z2 = p2

        u1, v1 = self.project_point(x1, y1, z1)
        u2, v2 = self.project_point(x2, y2, z2)

        # Check for degenerate edge
        if abs(u2 - u1) < 1e-9 and abs(v2 - v1) < 1e-9:
            return None

        return Edge2D(u1, v1, u2, v2, is_visible=True)

    def set_view_direction(self, direction: ViewDirection) -> None:
        """Set the view direction.

        Args:
            direction: New view direction
        """
        self.view_direction = direction
        self._init_projection()

    def get_depth(self, x: float, y: float, z: float) -> float:
        """Get depth value for occlusion sorting.

        Args:
            x: X coordinate
            y: Y coordinate
            z: Z coordinate

        Returns:
            Depth value (positive = closer to viewer)
        """
        if self.depth_axis == 'x':
            return x
        elif self.depth_axis == 'y':
            return y
        elif self.depth_axis == 'z':
            return z
        return 0


class HiddenLineDetector:
    """Detects hidden lines in orthographic projections."""

    def __init__(self, renderer: OrthographicRenderer):
        """Initialize hidden line detector.

        Args:
            renderer: Orthographic renderer
        """
        self.renderer = renderer

    def process_edges(self, edges: List[Edge2D],
                      all_points_3d: List[Tuple[float, float, float]]
                      ) -> List[Edge2D]:
        """Process edges and mark hidden lines.

        Uses simple depth-based detection for orthographic views.

        Args:
            edges: List of projected edges
            all_points_3d: Original 3D points for depth calculation

        Returns:
            Edges with visibility flags
        """
        if not edges:
            return edges

        # Build depth map
        edge_depths = []
        for i, edge in enumerate(edges):
            # Get approximate depth from edge endpoints
            # This is simplified - real implementation would use face info
            avg_depth = (edge.x1 + edge.x2) / 2  # Simplified
            edge_depths.append((avg_depth, i))

        # Sort by depth (back to front)
        edge_depths.sort(key=lambda x: x[0])

        # Mark visible edges
        visible = set()
        for depth, idx in edge_depths:
            edge = edges[idx]

            # Check if this edge is occluded by any visible edge
            occluded = False
            for vidx in visible:
                visible_edge = edges[vidx]
                if self._is_occluded(edge, visible_edge):
                    occluded = True
                    break

            if not occluded:
                visible.add(idx)
            else:
                edge.is_visible = False

        return edges

    def _is_occluded(self, test_edge: Edge2D, occluder: Edge2D) -> bool:
        """Check if test edge is occluded by occluder.

        Args:
            test_edge: Edge being tested
            occluder: Potentially occluding edge

        Returns:
            True if occluded
        """
        # Simple overlap check in 2D
        return self._edges_overlap_2d(test_edge, occluder)

    def _edges_overlap_2d(self, e1: Edge2D, e2: Edge2D) -> bool:
        """Check if edges overlap in projection.

        Args:
            e1: First edge
            e2: Second edge

        Returns:
            True if they overlap
        """
        # Parametric intersection test
        def ccw(ax, ay, bx, by, cx, cy):
            return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

        a1x, a1y, a2x, a2y = e1.x1, e1.y1, e1.x2, e1.y2
        b1x, b1y, b2x, b2y = e2.x1, e2.y1, e2.x2, e2.y2

        # Check if lines intersect
        return (ccw(a1x, a1y, b1x, b1y, b2x, b2y) != ccw(a2x, a2y, b1x, b1y, b2x, b2y) and
                ccw(a1x, a1y, a2x, a2y, b1x, b1y) != ccw(a1x, a1y, a2x, a2y, b2x, b2y))

    def process_silhouette(self, visible_edges: List[Edge2D],
                          hidden_edges: List[Edge2D]) -> List[Edge2D]:
        """Process silhouette edges with proper visibility.

        This method combines silhouette-computed visible and hidden edges
        with additional occlusion testing using depth information.

        Args:
            visible_edges: Edges computed as visible from silhouette
            hidden_edges: Edges computed as hidden from silhouette

        Returns:
            Combined list with correct visibility flags
        """
        result = []

        # For depth-based occlusion, we need the depth axis
        # Depth increases toward the viewer (closer = larger depth value)
        # - TOP view: depth axis is Z (height), larger Z = closer to viewer
        # - FRONT view: depth axis is Y (depth), larger Y = closer to viewer
        # - RIGHT view: depth axis is X (width), larger X = closer to viewer

        # First, add all visible edges (they're always visible)
        for edge in visible_edges:
            edge.is_visible = True
            result.append(edge)

        # Process hidden edges - check if they're actually occluded by visible edges
        for hidden_edge in hidden_edges:
            # Default to hidden
            hidden_edge.is_visible = False

            # Check if any visible edge occludes this hidden edge
            truly_hidden = False

            for vis_edge in visible_edges:
                # Check if edges overlap in 2D
                if not self._edges_overlap_2d(vis_edge, hidden_edge):
                    continue

                # If they overlap, check depth
                # The hidden edge is behind the visible edge if:
                # - They overlap in 2D projection
                # - The hidden edge has smaller depth value (farther from viewer)

                # Get depth values
                vis_depth = getattr(vis_edge, 'depth', None)
                hidden_depth = getattr(hidden_edge, 'depth', None)

                if vis_depth is not None and hidden_depth is not None:
                    # For all views: larger depth = closer to viewer
                    # If hidden_depth < vis_depth, it's behind
                    depth_behind = hidden_depth < vis_depth

                    if depth_behind:
                        truly_hidden = True
                        break

            # If this edge was marked as hidden from silhouette (not just 2D overlap),
            # preserve that state even if no 2D overlap was detected
            # (e.g., blind hole bottoms that don't overlap with visible edges)
            was_hidden_from_silhouette = getattr(hidden_edge, 'hidden_from_silhouette', False)

            hidden_edge.is_visible = not truly_hidden and not was_hidden_from_silhouette
            result.append(hidden_edge)

        return result

    def _edge_behind(self, front_edge: Edge2D, back_edge: Edge2D) -> bool:
        """Check if back_edge is behind front_edge (occluded).

        This is a simplified check - real CAD uses face information.

        Args:
            front_edge: Potentially occluding edge
            back_edge: Edge being tested

        Returns:
            True if back_edge is behind front_edge
        """
        # Check if edges overlap in 2D projection
        if not self._edges_overlap_2d(front_edge, back_edge):
            return False

        # For orthographic projection, we need depth information
        # This is simplified - we'd need actual depth values
        # For now, assume overlapping visible edges occlude hidden ones
        return True
