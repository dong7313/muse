"""SVG writer module for generating SVG files."""

from typing import List, Tuple, Optional
import math


class SVGWriter:
    """Handles SVG file generation."""

    def __init__(self, width: float, height: float, units: str = "mm"):
        """Initialize SVG writer.

        Args:
            width: Width of SVG in specified units
            height: Height of SVG in specified units
            units: Units for measurements (mm, cm, in)
        """
        self.width = width
        self.height = height
        self.units = units
        self.elements: List[str] = []
        self._defs: List[str] = []

    def add_viewport(self, x: float, y: float, width: float, height: float,
                     view_box: Optional[str] = None) -> None:
        """Add a viewport (clipping region) to the SVG.

        Args:
            x: X position
            y: Y position
            width: Viewport width
            height: Viewport height
            view_box: Optional viewBox for coordinate transformation
        """
        clip_id = f"clip_{len(self._defs)}"
        self._defs.append(f'''
    <clipPath id="{clip_id}">
      <rect x="{x}" y="{y}" width="{width}" height="{height}"/>
    </clipPath>
''')
        if view_box:
            self.elements.append(f'''<g clip-path="url(#{clip_id})">
  <g transform="translate({x}, {y})">
    <g viewBox="{view_box}">
''')
            self._viewport_depth = 3
        else:
            self.elements.append(f'''<g clip-path="url(#{clip_id})">
  <g transform="translate({x}, {y})">
''')
            self._viewport_depth = 2

    def end_viewport(self) -> None:
        """Close the current viewport group."""
        depth = getattr(self, '_viewport_depth', 2)
        if depth == 3:
            self.elements.append('''    </g>
  </g>
</g>
''')
        else:
            self.elements.append('''  </g>
</g>
''')

    def add_line(self, x1: float, y1: float, x2: float, y2: float,
                 stroke: str = "#000000", stroke_width: float = 0.5,
                 dash_array: Optional[str] = None) -> None:
        """Add a line element.

        Args:
            x1: Start X
            y1: Start Y
            x2: End X
            y2: End Y
            stroke: Stroke color
            stroke_width: Line width
            dash_array: Dash pattern (e.g., "5,5")
        """
        dash_attr = f' stroke-dasharray="{dash_array}"' if dash_array else ''
        self.elements.append(
            f'        <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{stroke_width}"{dash_attr}/>'
        )

    def add_polyline(self, points: List[Tuple[float, float]],
                     stroke: str = "#000000", stroke_width: float = 0.5,
                     fill: str = "none",
                     dash_array: Optional[str] = None) -> None:
        """Add a polyline element.

        Args:
            points: List of (x, y) coordinate tuples
            stroke: Stroke color
            stroke_width: Line width
            fill: Fill color
            dash_array: Dash pattern
        """
        if not points:
            return
        points_str = " ".join([f"{x},{y}" for x, y in points])
        dash_attr = f' stroke-dasharray="{dash_array}"' if dash_array else ''
        self.elements.append(
            f'        <polyline points="{points_str}" stroke="{stroke}" '
            f'stroke-width="{stroke_width}" fill="{fill}"{dash_attr}/>'
        )

    def add_path(self, d: str, stroke: str = "#000000",
                 stroke_width: float = 0.5, fill: str = "none",
                 dash_array: Optional[str] = None) -> None:
        """Add a path element.

        Args:
            d: Path data (SVG path commands)
            stroke: Stroke color
            stroke_width: Line width
            fill: Fill color
            dash_array: Dash pattern
        """
        dash_attr = f' stroke-dasharray="{dash_array}"' if dash_array else ''
        self.elements.append(
            f'        <path d="{d}" stroke="{stroke}" stroke-width="{stroke_width}" '
            f'fill="{fill}"{dash_attr}/>'
        )

    def add_text(self, x: float, y: float, text: str,
                 font_size: float = 3.5, font_family: str = "Arial, sans-serif",
                 fill: str = "#000000", text_anchor: str = "middle") -> None:
        """Add a text element.

        Args:
            x: X position
            y: Y position
            text: Text content
            font_size: Font size
            font_family: Font family
            fill: Text color
            text_anchor: Text anchor (start, middle, end)
        """
        self.elements.append(
            f'        <text x="{x}" y="{y}" font-size="{font_size}" '
            f'font-family="{font_family}" fill="{fill}" text-anchor="{text_anchor}">'
            f'{text}</text>'
        )

    def add_rect(self, x: float, y: float, width: float, height: float,
                 fill: str = "none", stroke: str = "#000000",
                 stroke_width: float = 0.5) -> None:
        """Add a rectangle element.

        Args:
            x: X position
            y: Y position
            width: Rectangle width
            height: Rectangle height
            fill: Fill color
            stroke: Stroke color
            stroke_width: Line width
        """
        self.elements.append(
            f'        <rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"/>'
        )

    def add_circle(self, cx: float, cy: float, r: float,
                   fill: str = "none", stroke: str = "#000000",
                   stroke_width: float = 0.5,
                   dash_array: Optional[str] = None) -> None:
        """Add a circle element.

        Args:
            cx: Center X
            cy: Center Y
            r: Radius
            fill: Fill color
            stroke: Stroke color
            stroke_width: Line width
            dash_array: Dash pattern (e.g., "5,5")
        """
        dash_attr = f' stroke-dasharray="{dash_array}"' if dash_array else ''
        self.elements.append(
            f'        <circle cx="{cx}" cy="{cy}" r="{r}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}"{dash_attr}/>'
        )

    def add_arc(self, cx: float, cy: float, r: float,
                start_angle: float, end_angle: float,
                stroke: str = "#000000", stroke_width: float = 0.5,
                dash_array: Optional[str] = None) -> None:
        """Add an arc element.

        Args:
            cx: Center X
            cy: Center Y
            r: Radius
            start_angle: Start angle in degrees
            end_angle: End angle in degrees
            stroke: Stroke color
            stroke_width: Line width
            dash_array: Dash pattern
        """
        start_rad = math.radians(start_angle)
        end_rad = math.radians(end_angle)

        x1 = cx + r * math.cos(start_rad)
        y1 = cy + r * math.sin(start_rad)
        x2 = cx + r * math.cos(end_rad)
        y2 = cy + r * math.sin(end_rad)

        large_arc = 1 if (end_angle - start_angle) > 180 else 0

        dash_attr = f' stroke-dasharray="{dash_array}"' if dash_array else ''

        self.elements.append(
            f'        <path d="M {x1} {y1} A {r} {r} 0 {large_arc} 1 {x2} {y2}" '
            f'stroke="{stroke}" stroke-width="{stroke_width}" fill="none"{dash_attr}/>'
        )

    def add_def(self, content: str) -> None:
        """Add custom definition to SVG defs section.

        Args:
            content: SVG definition content
        """
        self._defs.append(content)

    def add_raw(self, content: str) -> None:
        """Add raw SVG snippet directly.

        Args:
            content: Raw SVG fragment to append as-is.
        """
        self.elements.append(content)

    def render(self) -> str:
        """Render the complete SVG document.

        Returns:
            Complete SVG string
        """
        defs = f'<defs>{"".join(self._defs)}</defs>' if self._defs else ''

        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="{self.width}{self.units}"
     height="{self.height}{self.units}"
     viewBox="0 0 {self.width} {self.height}">
{defs}
{"".join(self.elements)}
</svg>'''
        return svg

    def write(self, filepath: str) -> None:
        """Write SVG to file.

        Args:
            filepath: Output file path
        """
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.render())
