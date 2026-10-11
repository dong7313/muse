"""Scale calculation utilities for technical drawings."""

from typing import Tuple, List, Optional
import math


class ScaleCalculator:
    """Calculates optimal scale for fitting models into viewports."""

    # Common engineering scales
    STANDARD_SCALES = [
        1, 2, 5, 10, 20, 50, 100,
        1/2, 1/5, 1/10, 1/20, 1/50, 1/100,
        1/200, 1/500, 1/1000
    ]

    def __init__(self, view_width: float, view_height: float,
                 margin: float = 0.1):
        """Initialize scale calculator.

        Args:
            view_width: Available width in mm
            view_height: Available height in mm
            margin: Margin as fraction (0.1 = 10%)
        """
        self.view_width = view_width
        self.view_height = view_height
        self.margin = margin

        self.available_width = view_width * (1 - 2 * margin)
        self.available_height = view_height * (1 - 2 * margin)

    def calculate_from_bbox(self, bbox: Tuple[float, float, float, float]
                            ) -> float:
        """Calculate scale to fit bounding box.

        Args:
            bbox: (min_x, min_y, max_x, max_y)

        Returns:
            Optimal scale
        """
        min_x, min_y, max_x, max_y = bbox

        width = max_x - min_x
        height = max_y - min_y

        if width < 1e-9:
            width = 1
        if height < 1e-9:
            height = 1

        scale_x = self.available_width / width
        scale_y = self.available_height / height

        return min(scale_x, scale_y)

    def calculate_from_bboxes(self, bboxes: List[Tuple[float, float, float, float]]
                              ) -> Tuple[float, float]:
        """Calculate scale to fit multiple bounding boxes.

        Args:
            bboxes: List of bounding boxes

        Returns:
            (scale, used_area_width, used_area_height)
        """
        if not bboxes:
            return (1.0, 0, 0)

        # Find combined bounding box
        min_x = min(b[0] for b in bboxes)
        min_y = min(b[1] for b in bboxes)
        max_x = max(b[2] for b in bboxes)
        max_y = max(b[3] for b in bboxes)

        # Calculate scale
        scale = self.calculate_from_bbox((min_x, min_y, max_x, max_y))

        # Calculate actual used dimensions
        used_width = (max_x - min_x) * scale
        used_height = (max_y - min_y) * scale

        return (scale, used_width, used_height)

    def get_standard_scale(self, calculated_scale: float,
                           prefer_larger: bool = False) -> float:
        """Get nearest standard scale.

        Args:
            calculated_scale: Calculated optimal scale
            prefer_larger: If True, prefer larger scale (zoom in)

        Returns:
            Nearest standard scale
        """
        if prefer_larger:
            # Find next larger or equal standard scale
            for s in sorted(self.STANDARD_SCALES):
                if s >= calculated_scale:
                    return s
            return self.STANDARD_SCALES[-1]  # Return largest
        else:
            # Find nearest standard scale
            nearest = min(self.STANDARD_SCALES,
                         key=lambda s: abs(s - calculated_scale))
            return nearest

    def get_fitting_standard_scale(self, calculated_scale: float) -> float:
        """Get the largest standard scale that still fits.

        This avoids choosing a scale larger than the calculated fit scale.
        """
        if calculated_scale <= 0:
            return self.STANDARD_SCALES[-1]

        candidates = sorted(s for s in self.STANDARD_SCALES if s <= calculated_scale)
        if candidates:
            return candidates[-1]
        return min(self.STANDARD_SCALES)

    def format_scale(self, scale: float) -> str:
        """Format scale as string for display.

        Args:
            scale: Scale factor

        Returns:
            Formatted scale string (e.g., "2:1" for enlargement, "1:2" for reduction)
        """
        if scale >= 1:
            # Enlargement: show as "X:1" (e.g., 2:1, 5:1)
            if scale == int(scale):
                return f"{int(scale)}:1"
            else:
                return f"{scale:.1f}:1"
        else:
            # Reduction: show as "1:X" (e.g., 1:2, 1:5)
            ratio = 1 / scale
            if ratio == int(ratio):
                return f"1:{int(ratio)}"
            else:
                return f"1:{ratio:.1f}"

    def calculate_viewport_positions(self, num_views: int = 4,
                                       cols: int = 2) -> List[Tuple[float, float, float, float]]:
        """Calculate viewport positions in a grid.

        Args:
            num_views: Number of viewports
            cols: Number of columns

        Returns:
            List of (x, y, width, height) tuples
        """
        rows = (num_views + cols - 1) // cols
        gap = 5.0  # Gap between viewports in mm

        cell_width = (self.view_width - (cols + 1) * gap) / cols
        cell_height = (self.view_height - (rows + 1) * gap) / rows

        positions = []
        for i in range(num_views):
            row = i // cols
            col = i % cols

            x = gap + col * (cell_width + gap)
            y = gap + row * (cell_height + gap)

            positions.append((x, y, cell_width, cell_height))

        return positions


class ModelAnalyzer:
    """Analyzes CadQuery models for dimension extraction."""

    @staticmethod
    def get_bounding_box(cad_object) -> Tuple[float, float, float, float, float, float]:
        """Get bounding box of CadQuery object.

        Args:
            cad_object: CadQuery Workplane or Solid

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)
        """
        # For Workplane, get the first solid's bounding box
        if hasattr(cad_object, 'val'):
            # It's a Workplane - get the underlying shape
            shape = cad_object.val()
        else:
            shape = cad_object

        if hasattr(shape, 'BoundingBox'):
            bbox = shape.BoundingBox()
        else:
            # Fallback: compute from vertices
            return (0, 0, 0, 1, 1, 1)

        return (
            bbox.xmin, bbox.ymin, bbox.zmin,
            bbox.xmax, bbox.ymax, bbox.zmax
        )

    @staticmethod
    def get_dimensions(bbox: Tuple[float, float, float, float, float, float]
                      ) -> Tuple[float, float, float]:
        """Get dimensions from bounding box.

        Args:
            bbox: (min_x, min_y, min_z, max_x, max_y, max_z)

        Returns:
            (width, height, depth)
        """
        min_x, min_y, min_z, max_x, max_y, max_z = bbox
        return (max_x - min_x, max_y - min_y, max_z - min_z)

    @staticmethod
    def get_center(bbox: Tuple[float, float, float, float, float, float]
                  ) -> Tuple[float, float, float]:
        """Get center point from bounding box.

        Args:
            bbox: Bounding box

        Returns:
            (center_x, center_y, center_z)
        """
        min_x, min_y, min_z, max_x, max_y, max_z = bbox
        return (
            (min_x + max_x) / 2,
            (min_y + max_y) / 2,
            (min_z + max_z) / 2
        )
