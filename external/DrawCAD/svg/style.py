"""SVG style definitions for technical drawings."""


class SVGStyle:
    """Contains style definitions for different line types."""

    # Standard line styles
    VISIBLE_LINE = {
        "stroke": "#000000",
        "stroke_width": 0.5,
        "fill": "none"
    }

    HIDDEN_LINE = {
        "stroke": "#000000",
        "stroke_width": 0.35,
        "dash_array": "5,2",
        "fill": "none"
    }

    CENTER_LINE = {
        "stroke": "#404040",
        "stroke_width": 0.35,
        "dash_array": "8,3,2,3",
        "fill": "none"
    }

    DIMENSION_LINE = {
        "stroke": "#000000",
        "stroke_width": 0.25,
        "fill": "none"
    }

    # Text styles
    TITLE_TEXT = {
        "font_size": 5,
        "font_family": "Arial, Helvetica, sans-serif",
        "fill": "#000000"
    }

    LABEL_TEXT = {
        "font_size": 3.5,
        "font_family": "Arial, Helvetica, sans-serif",
        "fill": "#000000"
    }

    SCALE_TEXT = {
        "font_size": 2.5,
        "font_family": "Arial, Helvetica, sans-serif",
        "fill": "#404040"
    }

    # Viewport styles
    VIEWPORT_BORDER = {
        "stroke": "#000000",
        "stroke_width": 0.7,
        "fill": "none"
    }

    VIEWPORT_FILL = {
        "fill": "#ffffff"
    }

    @staticmethod
    def get_visible_line(stroke_width: float = 0.5) -> dict:
        """Get visible line style with custom width.

        Args:
            stroke_width: Line width

        Returns:
            Style dictionary
        """
        style = SVGStyle.VISIBLE_LINE.copy()
        style["stroke_width"] = stroke_width
        return style

    @staticmethod
    def get_hidden_line(stroke_width: float = 0.35) -> dict:
        """Get hidden line style with custom width.

        Args:
            stroke_width: Line width

        Returns:
            Style dictionary
        """
        style = SVGStyle.HIDDEN_LINE.copy()
        style["stroke_width"] = stroke_width
        return style


class PaperLayout:
    """Paper layout constants for technical drawings."""

    SIZES = {
        "A2": (594.0, 420.0),
        "A3": (420.0, 297.0),
        "A4": (297.0, 210.0),
    }

    def __init__(self, paper_size: str = "A3"):
        normalized = (paper_size or "A3").upper()
        if normalized not in self.SIZES:
            raise ValueError(
                f"Unsupported paper size: {paper_size}. Use one of: {', '.join(self.SIZES)}"
            )

        self.PAPER_SIZE = normalized
        self.WIDTH, self.HEIGHT = self.SIZES[normalized]

        self.MARGIN = 10.0
        self.VIEW_GAP = 10.0
        self.TITLE_BLOCK_HEIGHT = 20.0
        # Keep a small but visible clearance from viewport borders.
        self.VIEW_INNER_MARGIN = 4.0
        # Native SVG fit ratio inside each viewport.
        self.VIEW_FIT_RATIO = 0.9
        # Final scale headroom so geometry nearly fills frame but does not touch border.
        self.SCALE_HEADROOM = 0.97

        self.VIEW_COLS = 2
        self.VIEW_ROWS = 2
        self.AVAILABLE_WIDTH = self.WIDTH - 2 * self.MARGIN
        self.AVAILABLE_HEIGHT = self.HEIGHT - 2 * self.MARGIN

        self.VIEW_WIDTH = (self.AVAILABLE_WIDTH - self.VIEW_GAP) / self.VIEW_COLS
        self.VIEW_HEIGHT = (
            self.AVAILABLE_HEIGHT - self.TITLE_BLOCK_HEIGHT - self.VIEW_GAP
        ) / self.VIEW_ROWS

        self.VIEW_POSITIONS = [
            (self.MARGIN, self.MARGIN),  # Top-left: Isometric
            (self.MARGIN + self.VIEW_WIDTH + self.VIEW_GAP, self.MARGIN),  # Top-right: Top
            (self.MARGIN, self.MARGIN + self.VIEW_HEIGHT + self.VIEW_GAP),  # Bottom-left: Front
            (
                self.MARGIN + self.VIEW_WIDTH + self.VIEW_GAP,
                self.MARGIN + self.VIEW_HEIGHT + self.VIEW_GAP,
            ),  # Bottom-right: Right
        ]

        self.VIEW_LABELS = [
            "Isometric",
            "Top",
            "Front",
            "Right",
        ]


class A3Layout(PaperLayout):
    """Backwards-compatible A3 layout wrapper."""

    def __init__(self):
        super().__init__("A3")
