"""Isometric view renderer."""

from typing import List, Tuple, Optional
import math
from .base import BaseRenderer, Edge2D


class IsometricRenderer(BaseRenderer):
    """Renders isometric (axonometric) views of 3D models.

    Standard isometric projection uses:
    - X axis: 330 degrees (or -30 degrees from horizontal)
    - Y axis: 210 degrees (or 150 degrees from horizontal)
    - Z axis: 90 degrees (vertical)
    """

    def __init__(self, view_width: float, view_height: float,
                 scale: float = 1.0, angle: float = 30.0):
        """Initialize isometric renderer.

        Args:
            view_width: Width of view area
            view_height: Height of view area
            scale: Drawing scale
            angle: Isometric angle in degrees (typically 30)
        """
        super().__init__(view_width, view_height, scale)
        self.angle = angle
        self._init_projection_matrices()

    def _init_projection_matrices(self):
        """Initialize isometric projection matrices."""
        # Standard isometric: 30 degrees from horizontal
        angle_rad = math.radians(self.angle)

        # Direction cosines for isometric axes
        # X axis: -30 degrees
        self.dir_x = (math.cos(angle_rad), math.sin(angle_rad), 0)

        # Y axis: 150 degrees (or 30 degrees below negative X)
        self.dir_y = (math.cos(math.pi - angle_rad), math.sin(math.pi - angle_rad), 0)

        # Z axis: vertical
        self.dir_z = (0, -1, 0)  # Negative for Y-down in SVG

    def project_point(self, x: float, y: float, z: float) -> Tuple[float, float]:
        """Project 3D point to isometric 2D coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            z: Z coordinate

        Returns:
            (u, v) 2D isometric coordinates
        """
        # Isometric projection matrix
        u = (x * self.dir_x[0] + y * self.dir_y[0] + z * self.dir_z[0])
        v = (x * self.dir_x[1] + y * self.dir_y[1] + z * self.dir_z[1])

        return (u, v)

    def project_edge(self, p1: Tuple[float, float, float],
                     p2: Tuple[float, float, float]) -> Optional[Edge2D]:
        """Project 3D edge to isometric view.

        Args: Start point (:
            p1x, y, z)
            p2: End point (x, y, z)

        Returns:
            Edge2D or None if degenerate
        """
        u1, v1 = self.project_point(*p1)
        u2, v2 = self.project_point(*p2)

        # Check for degenerate edge
        if abs(u2 - u1) < 1e-9 and abs(v2 - v1) < 1e-9:
            return None

        return Edge2D(u1, v1, u2, v2, is_visible=True)

    def set_view_angle(self, angle: float) -> None:
        """Set isometric viewing angle.

        Args:
            angle: New angle in degrees
        """
        self.angle = angle
        self._init_projection_matrices()

    def get_view_matrix(self) -> List[List[float]]:
        """Get the current view transformation matrix.

        Returns:
            4x4 transformation matrix
        """
        # Return as 2D projection (3x3 for 2D homogeneous)
        cos_a = math.cos(math.radians(self.angle))
        sin_a = math.sin(math.radians(self.angle))

        return [
            [cos_a, cos_a * 0.5, 0],
            [sin_a, -sin_a * 0.5, 0],
            [0, 0, 1]
        ]


class DimetricRenderer(IsometricRenderer):
    """Dimetric projection renderer (two equal angles)."""

    def __init__(self, view_width: float, view_height: float,
                 scale: float = 1.0, x_angle: float = 45.0, z_angle: float = 45.0):
        """Initialize dimetric renderer.

        Args:
            view_width: Width of view area
            view_height: Height of view area
            scale: Drawing scale
            x_angle: Angle for X axis from horizontal
            z_angle: Angle for Z axis from horizontal (Y is computed)
        """
        self.x_angle = x_angle
        self.z_angle = z_angle
        super().__init__(view_width, view_height, scale, x_angle)
        self._init_dimetric_matrices()

    def _init_dimetric_matrices(self):
        """Initialize dimetric projection matrices."""
        x_rad = math.radians(self.x_angle)
        z_rad = math.radians(self.z_angle)

        # X axis direction
        self.dir_x = (math.cos(x_rad), math.sin(x_rad), 0)

        # Z axis direction
        self.dir_z = (math.cos(z_rad), -math.sin(z_rad), 0)

        # Y axis: perpendicular in projection
        self.dir_y = (
            -self.dir_x[1] * self.dir_z[0] + self.dir_x[0] * self.dir_z[1],
            self.dir_x[0] * self.dir_z[0] + self.dir_x[1] * self.dir_z[1],
            0
        )
        # Normalize
        norm = math.sqrt(self.dir_y[0]**2 + self.dir_y[1]**2)
        if norm > 1e-9:
            self.dir_y = (self.dir_y[0]/norm, self.dir_y[1]/norm, 0)
