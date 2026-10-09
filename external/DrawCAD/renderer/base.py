"""Base renderer class for all view types."""

from typing import List, Tuple, Optional, Dict, Any
from abc import ABC, abstractmethod
import math
import numpy as np


class Edge2D:
    """Represents a 2D edge for rendering."""

    def __init__(self, x1: float, y1: float, x2: float, y2: float,
                 is_visible: bool = True):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.is_visible = is_visible

    def length(self) -> float:
        """Calculate edge length."""
        return math.sqrt((self.x2 - self.x1) ** 2 + (self.y2 - self.y1) ** 2)

    def midpoint(self) -> Tuple[float, float]:
        """Calculate midpoint."""
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def to_points(self) -> List[Tuple[float, float]]:
        """Convert to list of points."""
        return [(self.x1, self.y1), (self.x2, self.y2)]


class BaseRenderer(ABC):
    """Abstract base class for all view renderers."""

    def __init__(self, view_width: float, view_height: float,
                 scale: float = 1.0):
        """Initialize renderer.

        Args:
            view_width: Width of the view area
            view_height: Height of the view area
            scale: Drawing scale
        """
        self.view_width = view_width
        self.view_height = view_height
        self.scale = scale
        self.edges: List[Edge2D] = []
        self.bounding_box: Optional[Tuple[float, float, float, float]] = None

    @abstractmethod
    def project_point(self, x: float, y: float, z: float) -> Tuple[float, float]:
        """Project 3D point to 2D view coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            z: Z coordinate

        Returns:
            (u, v) 2D coordinates
        """
        pass

    @abstractmethod
    def project_edge(self, p1: Tuple[float, float, float],
                     p2: Tuple[float, float, float]) -> Optional[Edge2D]:
        """Project 3D edge to 2D view.

        Args:
            p1: Start point (x, y, z)
            p2: End point (x, y, z)

        Returns:
            Edge2D or None if edge is degenerate
        """
        pass

    def calculate_bounding_box(self, points: List[Tuple[float, float, float]]
                               ) -> Tuple[float, float, float, float]:
        """Calculate bounding box of 3D points.

        Args:
            points: List of 3D points

        Returns:
            (min_x, min_y, max_x, max_y)
        """
        if not points:
            return (0, 0, 0, 0)

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]

        return (min(xs), min(ys), max(xs), max(ys))

    def fit_to_view(self, bounding_box: Tuple[float, float, float, float],
                    margin: float = 0.05) -> float:
        """Calculate scale to fit bounding box to view.

        Args:
            bounding_box: (min_x, min_y, max_x, max_y)
            margin: Margin as fraction of view size

        Returns:
            Optimal scale factor
        """
        min_x, min_y, max_x, max_y = bounding_box

        width = max_x - min_x
        height = max_y - min_y

        if width < 1e-6:
            width = 1
        if height < 1e-6:
            height = 1

        available_width = self.view_width * (1 - 2 * margin)
        available_height = self.view_height * (1 - 2 * margin)

        scale_x = available_width / width
        scale_y = available_height / height

        return min(scale_x, scale_y)

    def center_offset(self, bounding_box: Tuple[float, float, float, float]
                      ) -> Tuple[float, float]:
        """Calculate offset to center content in view.

        Args:
            bounding_box: (min_x, min_y, max_x, max_y)

        Returns:
            (offset_x, offset_y) to center content
        """
        min_x, min_y, max_x, max_y = bounding_box

        width = max_x - min_x
        height = max_y - min_y

        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2

        # View center
        view_center_x = self.view_width / 2
        view_center_y = self.view_height / 2

        return (view_center_x - center_x, view_center_y - center_y)

    def process_cadquery_edges(self, edges, center: Tuple[float, float, float] = (0, 0, 0),
                               view_scale: float = 1.0) -> List[Edge2D]:
        """Process CadQuery edges to 2D edges.

        Args:
            edges: CadQuery edges
            center: Center point for centering
            view_scale: Additional scale factor

        Returns:
            List of Edge2D objects
        """
        result_edges = []

        # Get all vertices from edges
        vertices = []
        for edge in edges:
            verts = edge.Vertices()
            if verts:
                vertices.extend(verts)

        if not vertices:
            return result_edges

        # Calculate bounding box
        xs = [v.X for v in vertices]
        ys = [v.Y for v in vertices]
        zs = [v.Z for v in vertices]

        bbox_3d = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
        cx = (bbox_3d[0] + bbox_3d[3]) / 2
        cy = (bbox_3d[1] + bbox_3d[4]) / 2
        cz = (bbox_3d[2] + bbox_3d[5]) / 2

        # Project each edge
        for edge in edges:
            verts = edge.Vertices()
            if len(verts) >= 2:
                for i in range(len(verts) - 1):
                    p1 = verts[i]
                    p2 = verts[i + 1]

                    # Offset to center
                    x1 = (p1.X - cx) * view_scale
                    y1 = (p1.Y - cy) * view_scale
                    z1 = (p1.Z - cz) * view_scale

                    x2 = (p2.X - cx) * view_scale
                    y2 = (p2.Y - cy) * view_scale
                    z2 = (p2.Z - cz) * view_scale

                    # Project to 2D
                    projected = self.project_edge(
                        (x1, y1, z1), (x2, y2, z2)
                    )

                    if projected:
                        result_edges.append(projected)

        return result_edges

    def detect_hidden_lines(self, edges: List[Edge2D],
                             view_direction: Tuple[float, float, float] = (0, 0, 1)
                             ) -> List[Edge2D]:
        """Detect hidden lines in the projected edges.

        This is a simplified implementation. For more accurate hidden line
        detection, a proper 3D depth check would be needed.

        Args:
            edges: List of projected edges
            view_direction: Direction of view for occlusion detection

        Returns:
            Edges with visibility flag set
        """
        # Simple Z-buffer approach for orthographic views
        # Sort edges by their average depth
        depth_map: Dict[float, List[int]] = {}

        for i, edge in enumerate(edges):
            # Use average Z as depth key
            depth = (edge.x1 + edge.x2) / 2
            if depth not in depth_map:
                depth_map[depth] = []
            depth_map[depth].append(i)

        # Mark edges that are behind others as hidden
        sorted_depths = sorted(depth_map.keys())
        visible_indices = set()

        # Front-to-back: mark visible
        for depth in sorted_depths:
            for idx in depth_map[depth]:
                edge = edges[idx]
                # Check overlap with visible edges
                is_hidden = False

                # Simplified: check if edge overlaps with any visible edge
                for vidx in visible_indices:
                    visible = edges[vidx]
                    if self._edges_overlap(edge, visible):
                        is_hidden = True
                        break

                if not is_hidden:
                    visible_indices.add(idx)

        # Set visibility
        for i, edge in enumerate(edges):
            edge.is_visible = (i in visible_indices)

        return edges

    def _edges_overlap(self, e1: Edge2D, e2: Edge2D) -> bool:
        """Check if two edges overlap in projection.

        Args:
            e1: First edge
            e2: Second edge

        Returns:
            True if edges overlap
        """
        # Simple bounding box overlap check
        e1_min_x = min(e1.x1, e1.x2)
        e1_max_x = max(e1.x1, e1.x2)
        e1_min_y = min(e1.y1, e1.y2)
        e1_max_y = max(e1.y1, e1.y2)

        e2_min_x = min(e2.x1, e2.x2)
        e2_max_x = max(e2.x1, e2.x2)
        e2_min_y = min(e2.y1, e2.y2)
        e2_max_y = max(e2.y1, e2.y2)

        return not (e1_max_x < e2_min_x or e1_min_x > e2_max_x or
                    e1_max_y < e2_min_y or e1_min_y > e2_max_y)

    def get_edges_for_svg(self, edges: List[Edge2D],
                          offset_x: float = 0, offset_y: float = 0
                          ) -> Tuple[List[Tuple[Tuple[float, float], Tuple[float, float], bool]],
                                     List[Tuple[float, float]]]:
        """Get edges formatted for SVG output.

        Args:
            edges: List of Edge2D
            offset_x: X offset
            offset_y: Y offset

        Returns:
            Tuple of (edge_data, all_points)
        """
        edge_data = []
        all_points = []

        for edge in edges:
            p1 = (edge.x1 + offset_x, edge.y1 + offset_y)
            p2 = (edge.x2 + offset_x, edge.y2 + offset_y)
            edge_data.append((p1, p2, edge.is_visible))
            all_points.append(p1)
            all_points.append(p2)

        return edge_data, all_points
