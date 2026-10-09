"""Dimension annotation module for technical drawings.

Draws ISO 129 / ASME Y14.5 style dimension annotations on orthographic views:
- Overall bounding dimensions with extension lines and arrowheads
- Hole diameter annotations (⌀) with leader lines (Top view)
- Center marks and center lines for circular features
- Feature position dimensions from datum edges
- Center lines for cylinders in side views (Front/Right)
"""

import math
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from svg.writer import SVGWriter


class ModelBounds:
    """Model bounding rectangle in viewport-local coordinates."""

    def __init__(self, left: float, right: float, top: float, bottom: float):
        self.left = left
        self.right = right
        self.top = top
        self.bottom = bottom

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class CircularFeature:
    """A circular feature extracted from the 3D model."""
    center: Tuple[float, float, float]  # 3D center (x, y, z)
    radius: float                        # radius in mm
    axis: Tuple[float, float, float]     # axis direction
    is_hole: bool = True                 # True = hole, False = boss


@dataclass
class FilletFeature:
    """A fillet/chamfer feature."""
    radius: float
    center: Tuple[float, float, float]


@dataclass
class PocketFeature:
    """A rectangular pocket/cutout extracted from the 3D model."""
    center: Tuple[float, float, float]   # 3D center of pocket floor
    width: float                          # extent along first axis (mm)
    length: float                         # extent along second axis (mm)
    depth: float                          # pocket depth (mm)
    normal: Tuple[float, float, float]    # face normal of pocket floor


@dataclass
class StepFeature:
    """A step/shoulder at an intermediate height."""
    corner: Tuple[float, float, float]    # reference corner
    width: float                          # step width (mm)
    height: float                         # step height from base (mm)
    depth: float                          # step depth (mm)
    normal: Tuple[float, float, float]    # step face normal


@dataclass
class ModelFeatures:
    """All extracted features from a 3D model."""
    circles: List[CircularFeature] = field(default_factory=list)
    fillets: List[FilletFeature] = field(default_factory=list)
    pockets: List['PocketFeature'] = field(default_factory=list)
    steps: List['StepFeature'] = field(default_factory=list)
    bbox: Tuple[float, float, float, float, float, float] = (0, 0, 0, 0, 0, 0)


# ──────────────────── Style constants (SVG mm units) ────────────────────
DIM_COLOR = "#CC0000"
DIM_STROKE_WIDTH = 0.25
DIM_FONT_SIZE = 2.8
DIM_FONT_FAMILY = "Arial, sans-serif"
EXT_GAP = 1.5          # gap between model edge and extension line start
EXT_OVERSHOOT = 1.5    # extension line extends past dimension line
DIM_OFFSET = 10.0      # distance from model edge to 1st dimension line
DIM_OFFSET_2ND = 22.0  # distance for 2nd layer of dimensions
ARROW_LENGTH = 1.8
ARROW_HALF_W = 0.5
CENTER_MARK_SIZE = 1.5  # center mark half-size
CENTER_COLOR = "#CC0000"
CENTER_STROKE = 0.18
CENTER_DASH = "1.5,1"
LEADER_COLOR = "#CC0000"
LEADER_STROKE = 0.2
DIA_FONT_SIZE = 2.5
MIN_CIRCLE_R_VP = 2.5   # minimum viewport radius to annotate a circle


class ViewCoordMapper:
    """Maps 3D model coordinates to 2D viewport coordinates for a given view."""

    def __init__(
        self,
        view_label: str,
        bbox: Tuple[float, float, float, float, float, float],
        bounds: 'ModelBounds',
        view_x: float,
        view_y: float,
    ):
        self.view_label = view_label
        self.bbox = bbox
        self.bounds = bounds
        self.view_x = view_x
        self.view_y = view_y

        xmin, ymin, zmin, xmax, ymax, zmax = bbox
        if view_label == "Top":
            self.h_range = (xmin, xmax)
            self.v_range = (ymax, ymin)  # flipped: ymax→top, ymin→bottom
        elif view_label == "Front":
            self.h_range = (xmin, xmax)
            self.v_range = (zmax, zmin)
        elif view_label == "Right":
            self.h_range = (ymin, ymax)
            self.v_range = (zmax, zmin)
        else:
            self.h_range = (0, 1)
            self.v_range = (0, 1)

    def to_viewport(self, model_h: float, model_v: float) -> Tuple[float, float]:
        """Map model coordinates to absolute SVG coordinates."""
        h0, h1 = self.h_range
        v0, v1 = self.v_range
        dh = h1 - h0
        dv = v1 - v0
        if abs(dh) < 1e-9 or abs(dv) < 1e-9:
            return (self.bounds.cx + self.view_x, self.bounds.cy + self.view_y)

        t_h = (model_h - h0) / dh
        t_v = (model_v - v0) / dv

        vp_x = self.bounds.left + t_h * self.bounds.width
        vp_y = self.bounds.top + t_v * self.bounds.height
        return (self.view_x + vp_x, self.view_y + vp_y)

    def scale_to_viewport(self, model_length: float) -> float:
        """Convert a model-space length to viewport-space length."""
        h0, h1 = self.h_range
        dh = abs(h1 - h0)
        if dh < 1e-9:
            return 0.0
        return abs(model_length) * self.bounds.width / dh

    def get_3d_coords(self, feature: CircularFeature) -> Tuple[float, float]:
        """Extract the two model coordinates relevant to this view."""
        cx, cy, cz = feature.center
        if self.view_label == "Top":
            return (cx, cy)
        elif self.view_label == "Front":
            return (cx, cz)
        elif self.view_label == "Right":
            return (cy, cz)
        return (0, 0)

    def feature_visible_as_circle(self, feature: CircularFeature) -> bool:
        """Check if a circular feature appears as a circle in this view."""
        ax, ay, az = feature.axis
        if self.view_label == "Top":
            return abs(az) > 0.9
        elif self.view_label == "Front":
            return abs(ay) > 0.9
        elif self.view_label == "Right":
            return abs(ax) > 0.9
        return False

    def feature_visible_as_lines(self, feature: CircularFeature) -> bool:
        """Check if a circular feature appears as parallel lines (side view)."""
        ax, ay, az = feature.axis
        if self.view_label == "Top":
            return abs(az) < 0.1
        elif self.view_label == "Front":
            return abs(ay) < 0.1
        elif self.view_label == "Right":
            return abs(ax) < 0.1
        return False


def _fmt(val: float) -> str:
    """Format dimension value: strip trailing .0"""
    if val == int(val):
        return str(int(val))
    return f"{val:.1f}"


def _is_near(a: Tuple[float, float], b: Tuple[float, float], tol: float = 3.0) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


class DimensionAnnotator:
    """Draws dimension annotations on orthographic views."""

    def __init__(self, svg: SVGWriter):
        self.svg = svg

    # ─────────────── Public API ───────────────

    def annotate_view(
        self,
        view_label: str,
        view_x: float,
        view_y: float,
        view_w: float,
        view_h: float,
        bounds: ModelBounds,
        dim_h: float,
        dim_v: float,
        features: Optional[ModelFeatures] = None,
        dim_h_axis: str = "",
        dim_v_axis: str = "",
        annotated_dims: Optional[set] = None,
    ) -> None:
        """Add full dimension annotations to one orthographic view.

        Args:
            dim_h_axis / dim_v_axis: axis label ("X", "Y", "Z") for dedup.
            annotated_dims: shared set across views – dimensions/features
                already annotated are skipped to avoid repetition.
        """
        if view_label == "Isometric":
            return
        if bounds.width < 2 or bounds.height < 2:
            return
        if annotated_dims is None:
            annotated_dims = set()
        self._annotated_dims = annotated_dims

        left = view_x + bounds.left
        right = view_x + bounds.right
        top = view_y + bounds.top
        bottom = view_y + bounds.bottom

        # ── Overall bounding dimensions (skip if already annotated in another view) ──
        h_key = f"overall_{dim_h_axis}" if dim_h_axis else f"overall_h_{_fmt(dim_h)}"
        v_key = f"overall_{dim_v_axis}" if dim_v_axis else f"overall_v_{_fmt(dim_v)}"

        VIEW_LABEL_MARGIN = 10

        if h_key not in annotated_dims:
            h_text = _fmt(dim_h)
            dim_y = min(bottom + DIM_OFFSET, view_y + view_h - VIEW_LABEL_MARGIN)
            self._draw_horizontal_dim(left, right, dim_y, bottom, h_text)
            annotated_dims.add(h_key)

        if v_key not in annotated_dims:
            v_text = _fmt(dim_v)
            dim_x = min(right + DIM_OFFSET, view_x + view_w - 3)
            self._draw_vertical_dim(top, bottom, dim_x, right, v_text)
            annotated_dims.add(v_key)

        if features is None:
            return

        mapper = ViewCoordMapper(view_label, features.bbox, bounds, view_x, view_y)
        drawn_positions: list[Tuple[float, float, float]] = []
        self._annotation_placements: list[Tuple[float, float]] = []
        self._side_v_dim_count = 0
        self._side_h_dim_count = 0

        # ── Circular feature annotations ──
        # Sort by radius descending so larger (more important) features are annotated first.
        sorted_circles = sorted(features.circles, key=lambda f: f.radius, reverse=True)
        MAX_CIRCLE_ANNOT = 8       # max circle annotations per view
        MAX_SIDE_DIA_ANNOT = 4     # max side-view diameter annotations per view
        circle_count = 0
        side_dia_count = 0

        for feat in sorted_circles:
            if feat.radius < 1.5:
                continue
            # Skip bosses (external cylinders) — only annotate holes
            if not feat.is_hole:
                continue

            # Cross-view dedup key: same feature should only be dimensioned once
            feat_key = (round(feat.center[0], 1), round(feat.center[1], 1),
                        round(feat.center[2], 1), round(feat.radius, 1))

            if not mapper.feature_visible_as_circle(feat):
                # In side views, draw center lines and radius annotations
                if mapper.feature_visible_as_lines(feat):
                    self._draw_side_view_centerline(
                        feat, mapper, view_x, view_y, view_w, view_h, bounds,
                    )
                    # Draw radius annotation in Front/Right views
                    if view_label in ("Front", "Right") and side_dia_count < MAX_SIDE_DIA_ANNOT:
                        if feat_key not in annotated_dims:
                            self._draw_side_view_diameter(
                                feat, mapper, view_x, view_y, view_w, view_h,
                                drawn_positions, bounds,
                            )
                            side_dia_count += 1
                            annotated_dims.add(feat_key)
                continue

            if circle_count >= MAX_CIRCLE_ANNOT:
                continue
            # Skip if already dimensioned in a previous view
            if feat_key in annotated_dims:
                continue

            mh, mv = mapper.get_3d_coords(feat)
            vx, vy = mapper.to_viewport(mh, mv)
            r_vp = mapper.scale_to_viewport(feat.radius)

            if r_vp < MIN_CIRCLE_R_VP:
                continue
            # Use larger margin so leader lines/text don't extend beyond viewport
            if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 8):
                continue
            # Dedup: skip if any existing annotation with SIMILAR radius is nearby.
            # Allow concentric features (same position, different radius) to both
            # be annotated — e.g. Ø7 through-hole inside Ø13 counterbore.
            if any(abs(vx - dx) < 5 and abs(vy - dy) < 5
                   and abs(r_vp - dr) / max(r_vp, dr, 1) < 0.3
                   for dx, dy, dr in drawn_positions):
                continue

            drawn_positions.append((vx, vy, r_vp))
            circle_count += 1
            annotated_dims.add(feat_key)

            # Center mark + center lines
            self._draw_center_mark(vx, vy, r_vp, view_x, view_y, view_w, view_h)

            # Diameter annotation with leader line
            self._draw_diameter_annotation(
                vx, vy, r_vp, feat.radius * 2,
                view_x, view_y, view_w, view_h,
                self._annotation_placements,
            )

        # ── Position dimensions (Top view: both H and V from datum edges) ──
        if view_label == "Top" and drawn_positions:
            self._draw_position_dims(
                view_label, view_x, view_y, view_w, view_h,
                bounds, features, mapper, drawn_positions,
            )

        # ── Fillet radius annotations (Top view only) ──
        if view_label == "Top" and features.fillets:
            self._draw_fillet_annotations(
                view_x, view_y, view_w, view_h,
                bounds, features,
            )

        # ── Pocket dimension annotations ──
        if features.pockets:
            self._draw_pocket_annotations(
                view_label, mapper, view_x, view_y, view_w, view_h, bounds, features,
                annotated_dims,
            )

        # ── Step dimension annotations ──
        if features.steps:
            self._draw_step_annotations(
                view_label, mapper, view_x, view_y, view_w, view_h, bounds, features,
                annotated_dims,
            )

    # ─────────────── Overall dimension drawing ───────────────

    def _draw_horizontal_dim(
        self, x1: float, x2: float, dim_y: float, model_edge_y: float, text: str
    ) -> None:
        """Overall horizontal dim. Extension lines go from model edge toward dim_y."""
        svg = self.svg
        if dim_y > model_edge_y:
            ext_start = model_edge_y + EXT_GAP
            ext_end = dim_y + EXT_OVERSHOOT
        else:
            ext_start = model_edge_y - EXT_GAP
            ext_end = dim_y - EXT_OVERSHOOT
        svg.add_line(x1, ext_start, x1, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(x2, ext_start, x2, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(x1, dim_y, x2, dim_y, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_right(x2, dim_y)
        self._arrow_left(x1, dim_y)
        svg.add_text((x1 + x2) / 2, dim_y - 0.8, text,
                     font_size=DIM_FONT_SIZE, font_family=DIM_FONT_FAMILY,
                     fill=DIM_COLOR, text_anchor="middle")

    def _draw_vertical_dim(
        self, y1: float, y2: float, dim_x: float, model_edge_x: float, text: str
    ) -> None:
        """Overall vertical dim. Extension lines go from model edge toward dim_x."""
        svg = self.svg
        if dim_x > model_edge_x:
            ext_start = model_edge_x + EXT_GAP
            ext_end = dim_x + EXT_OVERSHOOT
        else:
            ext_start = model_edge_x - EXT_GAP
            ext_end = dim_x - EXT_OVERSHOOT
        svg.add_line(ext_start, y1, ext_end, y1, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(ext_start, y2, ext_end, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(dim_x, y1, dim_x, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_down(dim_x, y2)
        self._arrow_up(dim_x, y1)
        text_x = dim_x - 2.0
        text_y = (y1 + y2) / 2
        self.svg.add_raw(
            f'        <text x="{text_x}" y="{text_y}" '
            f'font-size="{DIM_FONT_SIZE}" font-family="{DIM_FONT_FAMILY}" '
            f'fill="{DIM_COLOR}" text-anchor="middle" '
            f'transform="rotate(-90, {text_x}, {text_y})">{text}</text>'
        )

    # ─────────────── Center marks and center lines ───────────────

    def _draw_center_mark(
        self, cx: float, cy: float, r_vp: float,
        view_x: float, view_y: float, view_w: float, view_h: float,
    ) -> None:
        """Draw center mark (cross) with extending center lines."""
        svg = self.svg
        m = CENTER_MARK_SIZE
        svg.add_line(cx - m, cy, cx + m, cy, stroke=CENTER_COLOR, stroke_width=CENTER_STROKE)
        svg.add_line(cx, cy - m, cx, cy + m, stroke=CENTER_COLOR, stroke_width=CENTER_STROKE)

        ext = r_vp + 3.0
        cl_x1 = max(cx - ext, view_x + 1)
        cl_x2 = min(cx + ext, view_x + view_w - 1)
        svg.add_line(cl_x1, cy, cl_x2, cy,
                     stroke=CENTER_COLOR, stroke_width=CENTER_STROKE, dash_array=CENTER_DASH)
        cl_y1 = max(cy - ext, view_y + 1)
        cl_y2 = min(cy + ext, view_y + view_h - 1)
        svg.add_line(cx, cl_y1, cx, cl_y2,
                     stroke=CENTER_COLOR, stroke_width=CENTER_STROKE, dash_array=CENTER_DASH)

    def _draw_side_view_centerline(
        self,
        feat: CircularFeature,
        mapper: ViewCoordMapper,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        bounds: Optional['ModelBounds'] = None,
    ) -> None:
        """Draw horizontal center line for a cylinder seen from the side."""
        mh, mv = mapper.get_3d_coords(feat)
        vx, vy = mapper.to_viewport(mh, mv)
        r_vp = mapper.scale_to_viewport(feat.radius)

        if r_vp < MIN_CIRCLE_R_VP:
            return
        if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
            return

        # Skip center lines that fall outside the model outline
        # (e.g. cylinder center near the top edge — line would appear above model)
        if bounds is not None:
            model_top = view_y + bounds.top
            model_bottom = view_y + bounds.bottom
            margin = 2.0
            if vy < model_top + margin or vy > model_bottom - margin:
                return

        # Small center mark
        m = CENTER_MARK_SIZE * 0.8
        self.svg.add_line(vx - m, vy, vx + m, vy,
                          stroke=CENTER_COLOR, stroke_width=CENTER_STROKE)
        self.svg.add_line(vx, vy - m, vx, vy + m,
                          stroke=CENTER_COLOR, stroke_width=CENTER_STROKE)

        # Horizontal center line through the cylinder width
        ext = r_vp + 2.5
        cl_x1 = max(vx - ext, view_x + 1)
        cl_x2 = min(vx + ext, view_x + view_w - 1)
        self.svg.add_line(cl_x1, vy, cl_x2, vy,
                          stroke=CENTER_COLOR, stroke_width=CENTER_STROKE,
                          dash_array=CENTER_DASH)

    def _draw_side_view_diameter(
        self,
        feat: CircularFeature,
        mapper: ViewCoordMapper,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        drawn_positions: list,
        bounds: ModelBounds = None,
    ) -> None:
        """Draw diameter dimension for a hole visible as hidden lines in a side view.

        Dimensions are placed OUTSIDE the model outline to avoid overlap.
        """
        mh, mv = mapper.get_3d_coords(feat)
        vx, vy = mapper.to_viewport(mh, mv)
        r_vp = mapper.scale_to_viewport(feat.radius)

        if r_vp < MIN_CIRCLE_R_VP:
            return
        if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 5):
            return

        # Aggressive dedup for side view
        for dx, dy, dr in drawn_positions:
            if abs(vx - dx) < 5 and abs(vy - dy) < 5:
                return
        drawn_positions.append((vx, vy, r_vp))

        diameter_mm = feat.radius * 2
        text = f"\u00D8{_fmt(diameter_mm)}"

        # Use model bounds for outside placement
        model_left = view_x + bounds.left if bounds else vx - r_vp
        model_right = view_x + bounds.right if bounds else vx + r_vp
        model_bottom = view_y + bounds.bottom if bounds else vy + r_vp

        ax, ay, az = feat.axis
        STAGGER_STEP = 7.0  # spacing between stacked dimension lines
        MIN_DIM_MARGIN = 8   # minimum margin from viewport edge for dim text
        # Geometry rule for side-view diameter direction:
        # - Z-axis hole in Front/Right: diameter visible as HORIZONTAL extent → h dim
        # - X-axis hole in Front: diameter visible as VERTICAL extent → v dim
        # - Y-axis hole in Right: diameter visible as VERTICAL extent → v dim
        if mapper.view_label == "Front":
            if abs(az) > 0.5:
                # Z-axis hole: diameter spans horizontally (X) in front view
                x_left = vx - r_vp   # left edge
                x_right = vx + r_vp  # right edge
                stagger = self._side_h_dim_count * STAGGER_STEP
                dim_y = min(model_bottom + DIM_OFFSET + stagger, view_y + view_h - 5)
                if dim_y > view_y + view_h - MIN_DIM_MARGIN:
                    return
                # Extension lines start from the feature position (vy) so they
                # connect to the hidden lines, not from model_bottom.
                ref_y = min(vy, model_bottom)
                self._draw_side_dia_dim_h(x_left, x_right, dim_y, ref_y, text)
                self._side_h_dim_count += 1
            elif abs(ax) > 0.5:
                # X-axis hole: diameter spans vertically (Z) in front view
                y_top = vy - r_vp   # top edge
                y_bot = vy + r_vp   # bottom edge
                stagger = self._side_v_dim_count * STAGGER_STEP
                dim_x = max(model_left - DIM_OFFSET - stagger, view_x + 3)
                if dim_x < view_x + MIN_DIM_MARGIN:
                    return
                ref_x = min(vx, model_left)
                self._draw_side_dia_dim_v(y_top, y_bot, dim_x, ref_x, text)
                self._side_v_dim_count += 1
        elif mapper.view_label == "Right":
            if abs(az) > 0.5:
                # Z-axis hole: diameter spans horizontally (Y) in right view
                x_left = vx - r_vp   # left edge
                x_right = vx + r_vp  # right edge
                stagger = self._side_h_dim_count * STAGGER_STEP
                dim_y = min(model_bottom + DIM_OFFSET + stagger, view_y + view_h - 5)
                if dim_y > view_y + view_h - MIN_DIM_MARGIN:
                    return
                ref_y = min(vy, model_bottom)
                self._draw_side_dia_dim_h(x_left, x_right, dim_y, ref_y, text)
                self._side_h_dim_count += 1
            elif abs(ay) > 0.5:
                # Y-axis hole: diameter spans vertically (Z) in right view
                y_top = vy - r_vp   # top edge
                y_bot = vy + r_vp   # bottom edge
                stagger = self._side_v_dim_count * STAGGER_STEP
                dim_x = max(model_left - DIM_OFFSET - stagger, view_x + 3)
                if dim_x < view_x + MIN_DIM_MARGIN:
                    return
                ref_x = min(vx, model_left)
                self._draw_side_dia_dim_v(y_top, y_bot, dim_x, ref_x, text)
                self._side_v_dim_count += 1

    def _draw_side_dia_dim_v(
        self, y1: float, y2: float, dim_x: float, ref_x: float, text: str
    ) -> None:
        """Draw a vertical diameter dimension for a side-view hole."""
        if y1 > y2:
            y1, y2 = y2, y1
        svg = self.svg
        # Extension lines from model edge to dim line (auto direction)
        if dim_x < ref_x:
            ext_start = ref_x - EXT_GAP
            ext_end = dim_x - EXT_OVERSHOOT
        else:
            ext_start = ref_x + EXT_GAP
            ext_end = dim_x + EXT_OVERSHOOT
        svg.add_line(ext_end, y1, ext_start, y1, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(ext_end, y2, ext_start, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(dim_x, y1, dim_x, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_up(dim_x, y1)
        self._arrow_down(dim_x, y2)
        text_x = dim_x - 2.0
        text_y = (y1 + y2) / 2
        self.svg.add_raw(
            f'        <text x="{text_x}" y="{text_y}" '
            f'font-size="{DIM_FONT_SIZE}" font-family="{DIM_FONT_FAMILY}" '
            f'fill="{DIM_COLOR}" text-anchor="middle" '
            f'transform="rotate(-90, {text_x}, {text_y})">{text}</text>'
        )

    def _draw_side_dia_dim_h(
        self, x1: float, x2: float, dim_y: float, ref_y: float, text: str
    ) -> None:
        """Draw a horizontal diameter dimension for a side-view hole."""
        if x1 > x2:
            x1, x2 = x2, x1
        svg = self.svg
        if dim_y > ref_y:
            ext_start = ref_y + EXT_GAP
            ext_end = dim_y + EXT_OVERSHOOT
        else:
            ext_start = ref_y - EXT_GAP
            ext_end = dim_y - EXT_OVERSHOOT
        svg.add_line(x1, ext_start, x1, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(x2, ext_start, x2, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        svg.add_line(x1, dim_y, x2, dim_y, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_left(x1, dim_y)
        self._arrow_right(x2, dim_y)
        svg.add_text((x1 + x2) / 2, dim_y - 0.8, text,
                      font_size=DIM_FONT_SIZE, font_family=DIM_FONT_FAMILY,
                      fill=DIM_COLOR, text_anchor="middle")

    # ─────────────── Diameter annotation ───────────────

    def _draw_diameter_annotation(
        self, cx: float, cy: float, r_vp: float, diameter_mm: float,
        view_x: float, view_y: float, view_w: float, view_h: float,
        annotation_placements: Optional[list] = None,
    ) -> None:
        """Draw ⌀ diameter annotation with leader line pointing outside model."""
        svg = self.svg
        text = f"\u00D8{_fmt(diameter_mm)}"

        # Determine the best direction to place the leader OUTSIDE the model
        # by choosing the direction toward the nearest viewport edge.
        vp_cx = view_x + view_w / 2
        vp_cy = view_y + view_h / 2

        placements = annotation_placements if annotation_placements is not None else []

        # Preferred angles: aim toward a viewport corner that has the most space
        candidates = []
        for angle_deg in [40, 135, -40, -135, 20, 160, -20, -160, 60, 120, -60, -120]:
            angle = math.radians(angle_deg)
            leader_len = max(r_vp + 10, 14)

            sx = cx + r_vp * math.cos(angle)
            sy = cy - r_vp * math.sin(angle)
            ex = cx + leader_len * math.cos(angle)
            ey = cy - leader_len * math.sin(angle)

            margin = 8
            in_bounds = (view_x + margin < ex < view_x + view_w - margin and
                         view_y + margin < ey < view_y + view_h - margin)
            if not in_bounds:
                continue

            # Check collision with existing annotation placements
            too_close = any(math.hypot(ex - px, ey - py) < 6
                           for px, py in placements)

            # Prefer directions that move AWAY from the viewport center
            # (i.e. toward the edges — outside the model)
            dist_from_center = math.hypot(ex - vp_cx, ey - vp_cy)
            # Penalize candidates that collide with existing annotations
            score = dist_from_center - (50 if too_close else 0)
            candidates.append((score, angle, sx, sy, ex, ey))

        if candidates:
            candidates.sort(reverse=True)  # pick farthest from center
            _, angle, sx, sy, ex, ey = candidates[0]
        else:
            # Fallback
            angle = math.radians(40)
            leader_len = max(r_vp + 8, 12)
            sx = cx + r_vp * math.cos(angle)
            sy = cy - r_vp * math.sin(angle)
            ex = cx + leader_len * math.cos(angle)
            ey = cy - leader_len * math.sin(angle)

        # Leader line
        svg.add_line(sx, sy, ex, ey, stroke=LEADER_COLOR, stroke_width=LEADER_STROKE)

        # Small dot at start
        svg.add_raw(
            f'        <circle cx="{sx}" cy="{sy}" r="0.4" '
            f'fill="{LEADER_COLOR}" stroke="none"/>'
        )

        # Horizontal shelf line
        shelf_dir = 1 if ex >= cx else -1
        shelf_end = ex + shelf_dir * 10
        # Clamp shelf to viewport
        if shelf_dir > 0:
            shelf_end = min(shelf_end, view_x + view_w - 2)
        else:
            shelf_end = max(shelf_end, view_x + 2)
        svg.add_line(ex, ey, shelf_end, ey, stroke=LEADER_COLOR, stroke_width=LEADER_STROKE)

        # Text above shelf
        text_anchor = "start" if shelf_dir > 0 else "end"
        text_x = ex + shelf_dir * 1
        svg.add_text(text_x, ey - 0.8, text,
                     font_size=DIA_FONT_SIZE, font_family=DIM_FONT_FAMILY,
                     fill=DIM_COLOR, text_anchor=text_anchor)

        # Record placement for collision avoidance
        placements.append((ex, ey))

    # ─────────────── Feature position dimensions ───────────────

    def _draw_position_dims(
        self,
        view_label: str,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        bounds: ModelBounds,
        features: ModelFeatures,
        mapper: ViewCoordMapper,
        drawn_circles: list,
    ) -> None:
        """Draw position dimensions for hole centers from datum edges.

        Draws both horizontal (from left edge) and vertical (from bottom edge)
        position dims for representative features.
        """
        if not drawn_circles:
            return

        left = view_x + bounds.left
        right = view_x + bounds.right
        top = view_y + bounds.top
        bottom = view_y + bounds.bottom

        xmin, ymin, zmin = features.bbox[0], features.bbox[1], features.bbox[2]
        xmax, ymax, zmax = features.bbox[3], features.bbox[4], features.bbox[5]
        bbox_w = xmax - xmin
        bbox_d = ymax - ymin

        candidates = []
        for feat in features.circles:
            if feat.radius < 1.0:
                continue
            if not mapper.feature_visible_as_circle(feat):
                continue

            mh, mv = mapper.get_3d_coords(feat)
            vx, vy = mapper.to_viewport(mh, mv)
            r_vp = mapper.scale_to_viewport(feat.radius)
            if r_vp < MIN_CIRCLE_R_VP:
                continue
            if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                continue

            fx, fy, fz = feat.center
            if view_label == "Top":
                dist_h = fx - xmin   # from left edge
                dist_v = fy - ymin   # from bottom edge (note: bottom in model = bottom in viewport because Y is flipped)
            else:
                continue

            # Skip if at center or at edges
            if abs(dist_h - bbox_w / 2) < 2:
                continue
            if dist_h < 1 or dist_h > bbox_w - 1:
                continue

            candidates.append((vx, vy, dist_h, dist_v, feat))

        if not candidates:
            return

        # Deduplicate by proximity
        unique = []
        for item in sorted(candidates, key=lambda c: c[2]):
            if not any(abs(item[0] - u[0]) < 4 and abs(item[1] - u[1]) < 4 for u in unique):
                unique.append(item)

        if not unique:
            return

        # ── Horizontal position dim (from left edge, above model) ──
        vx, vy, dist_h_mm, dist_v_mm, feat = unique[0]
        dim_y = top - DIM_OFFSET
        min_dim_y = view_y + 3
        if dim_y < min_dim_y:
            dim_y = min_dim_y

        if abs(vx - left) > 5:
            self._draw_h_pos_dim(left, vx, dim_y, top, _fmt(dist_h_mm))

        # ── Vertical position dim (from bottom edge, left of model) ──
        # In Top view, model bottom edge (ymin) maps to viewport bottom
        # We draw vertical dim on the left side of the model
        if abs(dist_v_mm) > 1 and abs(dist_v_mm - bbox_d) > 1:
            dim_x = left - DIM_OFFSET
            min_dim_x = view_x + 3
            if dim_x < min_dim_x:
                dim_x = min_dim_x

            if abs(vy - bottom) > 5:
                self._draw_v_pos_dim(bottom, vy, dim_x, left, _fmt(dist_v_mm))

        # ── Hole spacing: if there are 2+ holes at same Y, show spacing ──
        if len(unique) >= 2:
            # Find pairs at similar Y (same row of holes)
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    vi = unique[i]
                    vj = unique[j]
                    # Same vertical position (within tolerance)
                    if abs(vi[1] - vj[1]) < 3 and abs(vi[0] - vj[0]) > 8:
                        spacing_mm = abs(vi[2] - vj[2])
                        if spacing_mm > 2:
                            x1 = min(vi[0], vj[0])
                            x2 = max(vi[0], vj[0])
                            # Draw spacing dim slightly above the overall position dim
                            sp_y = dim_y - 5
                            if sp_y > view_y + 3:
                                mid_y = (vi[1] + vj[1]) / 2
                                self._draw_h_pos_dim(x1, x2, sp_y, mid_y - 2, _fmt(spacing_mm))
                        break  # only one spacing dim per view
                else:
                    continue
                break

    def _draw_h_pos_dim(self, x1: float, x2: float, dim_y: float, ref_y: float, text: str) -> None:
        """Draw a horizontal position dimension line.

        Extension lines run from ref_y toward dim_y (auto-detect direction).
        """
        # Extension lines go from model edge toward dimension line
        if dim_y > ref_y:
            # Dim is BELOW ref → extension goes downward
            ext_start = ref_y + EXT_GAP
            ext_end = dim_y + EXT_OVERSHOOT
        else:
            # Dim is ABOVE ref → extension goes upward
            ext_start = ref_y - EXT_GAP
            ext_end = dim_y - EXT_OVERSHOOT
        self.svg.add_line(x1, ext_start, x1, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self.svg.add_line(x2, ext_start, x2, ext_end, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self.svg.add_line(x1, dim_y, x2, dim_y, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_right(x2, dim_y)
        self._arrow_left(x1, dim_y)
        self.svg.add_text((x1 + x2) / 2, dim_y - 0.8, text,
                          font_size=DIM_FONT_SIZE, font_family=DIM_FONT_FAMILY,
                          fill=DIM_COLOR, text_anchor="middle")

    def _draw_v_pos_dim(self, y1: float, y2: float, dim_x: float, ref_x: float, text: str) -> None:
        """Draw a vertical position dimension line.

        Extension lines run from ref_x toward dim_x (auto-detect direction).
        """
        # Ensure y1 > y2 (y1 is bottom, larger SVG y)
        if y1 < y2:
            y1, y2 = y2, y1
        # Extension lines go from model edge toward dimension line
        if dim_x > ref_x:
            # Dim is RIGHT of ref → extension goes rightward
            ext_start = ref_x + EXT_GAP
            ext_end = dim_x + EXT_OVERSHOOT
        else:
            # Dim is LEFT of ref → extension goes leftward
            ext_start = ref_x - EXT_GAP
            ext_end = dim_x - EXT_OVERSHOOT
        self.svg.add_line(ext_end, y1, ext_start, y1, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self.svg.add_line(ext_end, y2, ext_start, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self.svg.add_line(dim_x, y1, dim_x, y2, stroke=DIM_COLOR, stroke_width=DIM_STROKE_WIDTH)
        self._arrow_down(dim_x, y1)
        self._arrow_up(dim_x, y2)
        text_x = dim_x - 2.0
        text_y = (y1 + y2) / 2
        self.svg.add_raw(
            f'        <text x="{text_x}" y="{text_y}" '
            f'font-size="{DIM_FONT_SIZE}" font-family="{DIM_FONT_FAMILY}" '
            f'fill="{DIM_COLOR}" text-anchor="middle" '
            f'transform="rotate(-90, {text_x}, {text_y})">{text}</text>'
        )

    def _draw_fillet_annotations(
        self,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        bounds: ModelBounds,
        features: ModelFeatures,
    ) -> None:
        """Draw fillet radius annotations (R...) near a corner of the model."""
        if not features.fillets:
            return

        # Collect unique fillet radii
        radii = sorted(set(round(f.radius, 1) for f in features.fillets))
        if not radii:
            return

        # Place fillet annotation text outside the model, below the bottom-left
        x = view_x + bounds.left
        y = view_y + bounds.bottom + DIM_OFFSET_2ND + 5
        # Clamp to viewport bottom
        y = min(y, view_y + view_h - 3)

        for i, r in enumerate(radii[:3]):  # max 3 fillet annotations
            text_y = y - i * 3.5
            if text_y < view_y + 2:
                break  # no room for more annotations
            text = f"R{_fmt(r)}"
            self.svg.add_text(x, text_y, text,
                              font_size=DIA_FONT_SIZE, font_family=DIM_FONT_FAMILY,
                              fill=DIM_COLOR, text_anchor="start")

    # ─────────────── Pocket annotations ───────────────

    def _draw_pocket_annotations(
        self,
        view_label: str,
        mapper: ViewCoordMapper,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        bounds: ModelBounds,
        features: ModelFeatures,
        annotated_dims: Optional[set] = None,
    ) -> None:
        """Draw dimension annotations for rectangular pockets.

        All dimensions are placed OUTSIDE the model outline to avoid overlap.
        """
        if annotated_dims is None:
            annotated_dims = set()
        model_right = view_x + bounds.right
        model_bottom = view_y + bounds.bottom
        model_top = view_y + bounds.top
        model_left = view_x + bounds.left

        for pocket in features.pockets:
            pocket_key = (round(pocket.center[0], 1), round(pocket.center[1], 1),
                          round(pocket.center[2], 1), "pocket")
            if pocket_key in annotated_dims:
                continue
            nx, ny, nz = pocket.normal

            if view_label == "Top" and abs(nz) > 0.5:
                cx, cy, cz = pocket.center
                mh, mv = cx, cy
                vx, vy = mapper.to_viewport(mh, mv)
                w_vp = mapper.scale_to_viewport(pocket.width)
                l_vp = mapper.scale_to_viewport(pocket.length)

                if w_vp < 3 or l_vp < 3:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                p_left = vx - w_vp / 2
                p_right = vx + w_vp / 2
                p_top = vy - l_vp / 2
                p_bottom = vy + l_vp / 2

                # Width dim — placed below the MODEL outline (not below the pocket)
                dim_y_pos = model_bottom + DIM_OFFSET_2ND
                if dim_y_pos < view_y + view_h - 10:
                    self._draw_h_pos_dim(p_left, p_right, dim_y_pos, model_bottom, _fmt(pocket.width))

                # Length dim — placed right of the MODEL outline
                dim_x_pos = model_right + DIM_OFFSET_2ND
                if dim_x_pos < view_x + view_w - 5:
                    self._draw_v_pos_dim(p_bottom, p_top, dim_x_pos, model_right, _fmt(pocket.length))
                annotated_dims.add(pocket_key)

            elif view_label == "Front" and abs(nz) > 0.5:
                cx, cy, cz = pocket.center
                mh, mv = cx, cz
                vx, vy = mapper.to_viewport(mh, mv)
                d_vp = mapper.scale_to_viewport(pocket.depth)

                if d_vp < 2:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                # Depth dim — placed right of the MODEL outline
                top_edge = model_top
                pocket_floor_y = top_edge + d_vp
                dim_x_pos = model_right + DIM_OFFSET_2ND
                if dim_x_pos < view_x + view_w - 5 and d_vp > 3:
                    self._draw_v_pos_dim(pocket_floor_y, top_edge, dim_x_pos, model_right, _fmt(pocket.depth))
                annotated_dims.add(pocket_key)

            elif view_label == "Right" and abs(nz) > 0.5:
                cx, cy, cz = pocket.center
                mh, mv = cy, cz
                vx, vy = mapper.to_viewport(mh, mv)
                d_vp = mapper.scale_to_viewport(pocket.depth)

                if d_vp < 2:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                top_edge = model_top
                pocket_floor_y = top_edge + d_vp
                dim_x_pos = model_right + DIM_OFFSET_2ND
                if dim_x_pos < view_x + view_w - 5 and d_vp > 3:
                    self._draw_v_pos_dim(pocket_floor_y, top_edge, dim_x_pos, model_right, _fmt(pocket.depth))
                annotated_dims.add(pocket_key)

            elif view_label == "Front" and abs(ny) > 0.5:
                cx, cy, cz = pocket.center
                mh, mv = cx, cz
                vx, vy = mapper.to_viewport(mh, mv)
                w_vp = mapper.scale_to_viewport(pocket.width)
                d_vp = mapper.scale_to_viewport(pocket.depth)

                if w_vp < 3 or d_vp < 2:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                p_left = vx - w_vp / 2
                p_right = vx + w_vp / 2
                p_top = vy - d_vp / 2
                p_bottom = vy + d_vp / 2

                # Width dim below MODEL outline
                dim_y_pos = model_bottom + DIM_OFFSET_2ND
                if dim_y_pos < view_y + view_h - 5:
                    self._draw_h_pos_dim(p_left, p_right, dim_y_pos, model_bottom, _fmt(pocket.width))

                # Depth dim right of MODEL outline
                dim_x_pos = model_right + DIM_OFFSET_2ND
                if dim_x_pos < view_x + view_w - 5:
                    self._draw_v_pos_dim(p_bottom, p_top, dim_x_pos, model_right, _fmt(pocket.depth))
                annotated_dims.add(pocket_key)

    # ─────────────── Step annotations ───────────────

    def _draw_step_annotations(
        self,
        view_label: str,
        mapper: ViewCoordMapper,
        view_x: float, view_y: float,
        view_w: float, view_h: float,
        bounds: ModelBounds,
        features: ModelFeatures,
        annotated_dims: Optional[set] = None,
    ) -> None:
        """Draw dimension annotations for step/shoulder features.

        All dimensions are placed OUTSIDE the model outline.
        """
        if annotated_dims is None:
            annotated_dims = set()
        model_right = view_x + bounds.right
        model_bottom = view_y + bounds.bottom
        model_top = view_y + bounds.top
        model_left = view_x + bounds.left

        for step in features.steps:
            step_key = (round(step.corner[0], 1), round(step.corner[1], 1),
                        round(step.corner[2], 1), "step")
            if step_key in annotated_dims:
                continue
            nx, ny, nz = step.normal

            if view_label == "Front" and abs(nz) > 0.5:
                cx, cy, cz = step.corner
                mh, mv = cx, cz
                vx, vy = mapper.to_viewport(mh, mv)
                h_vp = mapper.scale_to_viewport(step.height)
                w_vp = mapper.scale_to_viewport(step.width)

                if h_vp < 2:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                # Step height dim — placed LEFT of the model outline
                bottom_y = model_bottom
                step_y = bottom_y - h_vp
                dim_x_pos = model_left - DIM_OFFSET_2ND
                if dim_x_pos > view_x + 3 and h_vp > 3:
                    self._draw_v_pos_dim(bottom_y, step_y, dim_x_pos, model_left, _fmt(step.height))
                annotated_dims.add(step_key)

            elif view_label == "Right" and abs(nz) > 0.5:
                cx, cy, cz = step.corner
                mh, mv = cy, cz
                vx, vy = mapper.to_viewport(mh, mv)
                h_vp = mapper.scale_to_viewport(step.height)

                if h_vp < 2:
                    continue
                if not self._in_viewport(vx, vy, view_x, view_y, view_w, view_h, 3):
                    continue

                # Step height dim — placed RIGHT of the model outline
                bottom_y = model_bottom
                step_y = bottom_y - h_vp
                dim_x_pos = model_right + DIM_OFFSET_2ND
                if dim_x_pos < view_x + view_w - 5 and h_vp > 3:
                    self._draw_v_pos_dim(bottom_y, step_y, dim_x_pos, model_right, _fmt(step.height))
                annotated_dims.add(step_key)

    # ─────────────── Utilities ───────────────

    def _in_viewport(
        self, x: float, y: float,
        view_x: float, view_y: float, view_w: float, view_h: float,
        margin: float = 2,
    ) -> bool:
        return (view_x + margin < x < view_x + view_w - margin and
                view_y + margin < y < view_y + view_h - margin)

    # ─────────────── Arrowheads ───────────────

    def _arrow_right(self, x: float, y: float) -> None:
        x1 = x - ARROW_LENGTH
        self.svg.add_raw(
            f'        <polygon points="{x},{y} {x1},{y - ARROW_HALF_W} {x1},{y + ARROW_HALF_W}" '
            f'fill="{DIM_COLOR}" stroke="none"/>'
        )

    def _arrow_left(self, x: float, y: float) -> None:
        x1 = x + ARROW_LENGTH
        self.svg.add_raw(
            f'        <polygon points="{x},{y} {x1},{y - ARROW_HALF_W} {x1},{y + ARROW_HALF_W}" '
            f'fill="{DIM_COLOR}" stroke="none"/>'
        )

    def _arrow_up(self, x: float, y: float) -> None:
        y1 = y + ARROW_LENGTH
        self.svg.add_raw(
            f'        <polygon points="{x},{y} {x - ARROW_HALF_W},{y1} {x + ARROW_HALF_W},{y1}" '
            f'fill="{DIM_COLOR}" stroke="none"/>'
        )

    def _arrow_down(self, x: float, y: float) -> None:
        y1 = y - ARROW_LENGTH
        self.svg.add_raw(
            f'        <polygon points="{x},{y} {x - ARROW_HALF_W},{y1} {x + ARROW_HALF_W},{y1}" '
            f'fill="{DIM_COLOR}" stroke="none"/>'
        )
