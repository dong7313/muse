#!/usr/bin/env python3
"""
CadQuery to SVG Converter

Converts CadQuery 3D models to SVG technical drawings with:
- Isometric view (top-left)
- Top view (top-right)
- Front view (bottom-left)
- Right view (bottom-right)
- Hidden line detection
- Scale annotations
"""

import sys
import argparse
from typing import Any, Optional, Tuple, List
import importlib.util
from pathlib import Path
import re
import math
import xml.etree.ElementTree as ET

# Import CadQuery
try:
    import cadquery
    from cadquery import cq, Edge, Solid
except ImportError:
    print("Error: CadQuery is not installed. Install with: pip install cadquery")
    sys.exit(1)

try:
    from cadquery.occ_impl.exporters import svg as cq_svg_export
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
except Exception:
    cq_svg_export = None

# Import local modules
from svg.writer import SVGWriter
from svg.style import SVGStyle, PaperLayout
from svg.dimensions import (
    DimensionAnnotator, ModelBounds, ModelFeatures, CircularFeature,
    PocketFeature, StepFeature,
)
from renderer.isometric import IsometricRenderer
from renderer.orthographic import OrthographicRenderer, ViewDirection, HiddenLineDetector
from renderer.base import Edge2D
from utils.scale import ScaleCalculator, ModelAnalyzer


class CadQueryToSVG:
    """Main converter class for CadQuery to SVG."""

    def __init__(self, paper_size: str = "A3", name: str = "CadQuery to SVG"):
        """Initialize converter.

        Args:
            paper_size: Paper size (A3, A4, etc.)
            name: Drawing title/name
        """
        self.paper_size = (paper_size or "A3").upper()
        self.layout = PaperLayout(self.paper_size)
        self.model: Optional[cadquery.Workplane] = None
        self.scale = 1.0
        self.model_center = (0, 0, 0)
        self.name = name
        # Camera directions for CadQuery native SVG export.
        # These can be tuned if your drafting convention is different.
        self.view_projection_dirs = {
            # Use a canonical isometric direction so Z-up features appear upward.
            "isometric": (1.0, 1.0, 1.0),
            "top": (0.0, 0.0, 1.0),
            "front": (0.0, 1.0, 0.0),
            "right": (1.0, 0.0, 0.0),
        }

    def set_name(self, name: str) -> 'CadQueryToSVG':
        """Set the drawing title/name.

        Args:
            name: Drawing title

        Returns:
            Self for chaining
        """
        self.name = name
        return self

    @staticmethod
    def _normalize_model(value):
        """Coerce various CadQuery return shapes into something the renderer can use.

        Handles:
          - cadquery.Assembly       → assy.toCompound() → Workplane wrapping Compound
          - cadquery.Compound/Solid/Shape (any object with .wrapped) → wrap in Workplane
          - list / tuple of the above (or (shape, label) tuples) → flatten Solids → Compound
          - cadquery.Workplane with multiple stack objects → flatten into single Compound
          - cadquery.Workplane with a single object → as-is
          - None or unknown → returned unchanged (caller may still raise)
        """
        if value is None:
            return None

        # Assembly: walk the tree, apply transforms, flatten into a single Compound
        if isinstance(value, cadquery.Assembly):
            try:
                value = value.toCompound()
            except Exception:
                return value  # fall through to existing failure

        # list / tuple: extract Solids, then build a Compound
        if isinstance(value, (list, tuple)):
            solids = []
            for item in value:
                # accept (shape, label) / (shape, location, ...) tuples
                if isinstance(item, tuple) and item:
                    item = item[0]
                if isinstance(item, cadquery.Assembly):
                    try:
                        item = item.toCompound()
                    except Exception:
                        continue
                if isinstance(item, cadquery.Workplane):
                    if hasattr(item, 'vals'):
                        for v in item.vals():
                            if hasattr(v, 'Solids'):
                                solids.extend(v.Solids())
                            elif hasattr(v, 'wrapped'):
                                solids.append(v)
                elif hasattr(item, 'Solids'):
                    solids.extend(item.Solids())
                elif hasattr(item, 'wrapped'):
                    solids.append(item)
            if solids:
                comp = cadquery.Compound.makeCompound(solids)
                return cadquery.Workplane('XY').newObject([comp])
            return value  # nothing extractable; let downstream raise

        # Workplane: flatten if multi-object on the stack
        if isinstance(value, cadquery.Workplane):
            objs = list(value.objects) if value.objects else []
            if len(objs) > 1:
                solids = []
                for obj in objs:
                    if hasattr(obj, 'Solids'):
                        solids.extend(obj.Solids())
                    elif hasattr(obj, 'wrapped'):
                        solids.append(obj)
                if solids:
                    comp = cadquery.Compound.makeCompound(solids)
                    return cadquery.Workplane('XY').newObject([comp])
            return value

        # Bare Solid / Compound / Shape: wrap into a Workplane for uniform handling
        if hasattr(value, 'wrapped'):
            return cadquery.Workplane('XY').newObject([value])

        return value  # unknown type; let caller fail with a clearer message

    def load_model_from_file(self, filepath: str) -> 'CadQueryToSVG':
        """Load model from file.

        Args:
            filepath: Path to CadQuery Python file or STEP file

        Returns:
            Self for chaining
        """
        source_path = Path(filepath).expanduser().resolve()
        suffix = source_path.suffix.lower()
        if suffix in (".stp", ".step"):
            return self.load_model_from_step(str(source_path))
        if suffix != ".py":
            raise ValueError(f"Unsupported model file type: {source_path.suffix}")

        # Load module from file
        spec = importlib.util.spec_from_file_location("cad_model", str(source_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Look for model object
        if hasattr(module, 'result'):
            self.model = self._normalize_model(module.result)
        elif hasattr(module, 'model'):
            self.model = self._normalize_model(module.model)
        elif hasattr(module, 'shape'):
            self.model = self._normalize_model(module.shape)
        else:
            # Try to find any Workplane/Solid/Compound/Assembly in module
            for name in dir(module):
                obj = getattr(module, name)
                if isinstance(obj, (cadquery.Workplane, cadquery.Solid,
                                     cadquery.Compound, cadquery.Assembly)):
                    self.model = self._normalize_model(obj)
                    break

        if self.model is None:
            raise ValueError(f"No CadQuery model found in {source_path}")

        return self

    def load_model_from_step(self, filepath: str) -> 'CadQueryToSVG':
        """Load CadQuery model from STEP file."""
        source_path = Path(filepath).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"STEP file not found: {source_path}")
        self.model = self._normalize_model(cadquery.importers.importStep(str(source_path)))
        return self

    def load_model_from_code(self, code: str) -> 'CadQueryToSVG':
        """Load CadQuery model from code string."""
        namespace: dict[str, Any] = {"cadquery": cadquery, "cq": cadquery}
        exec(code, namespace, namespace)

        if "result" in namespace:
            self.model = self._normalize_model(namespace["result"])
        elif "model" in namespace:
            self.model = self._normalize_model(namespace["model"])
        elif "shape" in namespace:
            self.model = self._normalize_model(namespace["shape"])
        else:
            for _, obj in namespace.items():
                if isinstance(obj, (cadquery.Workplane, cadquery.Solid,
                                     cadquery.Compound, cadquery.Assembly)):
                    self.model = self._normalize_model(obj)
                    break

        if self.model is None:
            raise ValueError("No CadQuery model found in code string.")
        return self

    def load_model(self, model) -> 'CadQueryToSVG':
        """Load CadQuery model directly.

        Args:
            model: CadQuery Workplane / Solid / Compound / Assembly / list / tuple

        Returns:
            Self for chaining
        """
        self.model = self._normalize_model(model)
        return self

    def _analyze_model(self) -> Tuple[float, float, float]:
        """Analyze model to get dimensions and center.

        Returns:
            (width, height, depth)
        """
        if self.model is None:
            return (100, 100, 100)

        # Get bounding box
        analyzer = ModelAnalyzer()
        bbox = analyzer.get_bounding_box(self.model)
        dimensions = analyzer.get_dimensions(bbox)
        self.model_center = analyzer.get_center(bbox)

        # Store bounding box for silhouette computation
        # bbox is (xmin, ymin, zmin, xmax, ymax, zmax)
        self._model_bbox = bbox

        return dimensions

    def _extract_features(self) -> ModelFeatures:
        """Extract geometric features (holes, bosses, fillets) from the model.

        Uses OCC topology to find cylindrical faces and circular edges.
        """
        features = ModelFeatures(bbox=getattr(self, '_model_bbox', (0, 0, 0, 0, 0, 0)))

        if self.model is None:
            return features

        shape = self.model.val() if hasattr(self.model, 'val') else self.model

        try:
            from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
            from OCP.TopExp import TopExp_Explorer
            from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Circle
            from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
            from OCP.TopoDS import TopoDS
        except ImportError:
            return features

        # Track unique cylindrical features by projected position and radius.
        # For Z-axis holes, key by (x, y, r) ignoring z; similarly for other axes.
        seen_cyls = set()
        seen_cyls_proj = set()  # deduplicate by axis-perpendicular coords
        # Collect fillet radii to exclude them from hole annotations
        fillet_radii = set()

        try:
            from OCP.GeomAbs import GeomAbs_Torus
            exp_fillet = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
            while exp_fillet.More():
                face = TopoDS.Face_s(exp_fillet.Current())
                surf = BRepAdaptor_Surface(face)
                if surf.GetType() == GeomAbs_Torus:
                    torus = surf.Torus()
                    fillet_radii.add(round(torus.MinorRadius(), 1))
                exp_fillet.Next()
        except ImportError:
            pass

        # Extract cylindrical faces → holes / bosses
        # Use angular span to skip partial-arc cylinders (fillets/chamfers are
        # typically 90° arcs; real holes/bosses are full 360° cylinders).
        # Use BRepClass3d_SolidClassifier for reliable hole-vs-boss detection
        # instead of face orientation which is unreliable after Boolean ops.
        import math as _math
        from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
        try:
            from OCP.BRepClass3d import BRepClass3d_SolidClassifier
            from OCP.TopAbs import TopAbs_IN
            from OCP.gp import gp_Pnt
            _have_classifier = True
        except ImportError:
            _have_classifier = False

        known_cyl_radii = set()  # track radii from cylindrical faces

        exp = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
        while exp.More():
            face = TopoDS.Face_s(exp.Current())
            surf = BRepAdaptor_Surface(face)
            if surf.GetType() == GeomAbs_Cylinder:
                cyl = surf.Cylinder()
                ax = cyl.Axis()
                loc = ax.Location()
                d = ax.Direction()
                r = cyl.Radius()
                # Skip fillet cylinders (radius matches a fillet)
                if round(r, 1) in fillet_radii:
                    exp.Next()
                    continue
                # Skip partial-arc cylinders (fillets on straight edges are ~90°)
                u_span = abs(surf.LastUParameter() - surf.FirstUParameter())
                if u_span < _math.pi * 1.5:  # less than ~270° → fillet/blend
                    exp.Next()
                    continue

                # Determine hole vs boss using solid classifier
                if _have_classifier:
                    # Sample a point on the face, offset outward from axis
                    u_mid = (surf.FirstUParameter() + surf.LastUParameter()) / 2
                    v_mid = (surf.FirstVParameter() + surf.LastVParameter()) / 2
                    pnt = surf.Value(u_mid, v_mid)
                    ax_loc = ax.Location()
                    ax_dir = d
                    # Vector from axis to surface point (radial outward)
                    rx = pnt.X() - ax_loc.X()
                    ry = pnt.Y() - ax_loc.Y()
                    rz = pnt.Z() - ax_loc.Z()
                    # Remove axis-parallel component
                    dot = rx * ax_dir.X() + ry * ax_dir.Y() + rz * ax_dir.Z()
                    rx -= dot * ax_dir.X()
                    ry -= dot * ax_dir.Y()
                    rz -= dot * ax_dir.Z()
                    rl = _math.sqrt(rx*rx + ry*ry + rz*rz)
                    if rl > 1e-9:
                        rx /= rl; ry /= rl; rz /= rl
                    # Test point slightly outward from the cylindrical surface
                    eps = 0.3
                    test_pt = gp_Pnt(pnt.X() + eps * rx, pnt.Y() + eps * ry, pnt.Z() + eps * rz)
                    classifier = BRepClass3d_SolidClassifier(shape.wrapped, test_pt, 1e-6)
                    face_is_hole = (classifier.State() == TopAbs_IN)
                else:
                    face_is_hole = face.Orientation() == TopAbs_REVERSED

                key = (round(loc.X(), 1), round(loc.Y(), 1), round(loc.Z(), 1), round(r, 1))
                if key in seen_cyls:
                    exp.Next()
                    continue
                seen_cyls.add(key)
                known_cyl_radii.add(round(r, 1))
                # Deduplicate by projected position: for Z-axis holes use (x,y,r)
                dx, dy, dz = d.X(), d.Y(), d.Z()
                if abs(dz) > 0.5:
                    proj_key = (round(loc.X(), 0), round(loc.Y(), 0), round(r, 0))
                elif abs(dx) > 0.5:
                    proj_key = (round(loc.Y(), 0), round(loc.Z(), 0), round(r, 0))
                else:
                    proj_key = (round(loc.X(), 0), round(loc.Z(), 0), round(r, 0))
                if proj_key not in seen_cyls_proj:
                    seen_cyls_proj.add(proj_key)
                    features.circles.append(CircularFeature(
                        center=(loc.X(), loc.Y(), loc.Z()),
                        radius=r,
                        axis=(dx, dy, dz),
                        is_hole=face_is_hole,
                    ))
            exp.Next()

        # Also extract circular edges for features not captured by cylindrical faces.
        # Only add edges whose radius matches a known cylindrical face radius
        # to avoid chamfer/blend artifacts (e.g. r±chamfer_size).
        seen_circles = set()
        exp2 = TopExp_Explorer(shape.wrapped, TopAbs_EDGE)
        while exp2.More():
            edge = TopoDS.Edge_s(exp2.Current())
            curve = BRepAdaptor_Curve(edge)
            if curve.GetType() == GeomAbs_Circle:
                circ = curve.Circle()
                loc = circ.Location()
                r = circ.Radius()
                # Skip fillet arcs
                if round(r, 1) in fillet_radii:
                    exp2.Next()
                    continue
                # Skip chamfer artifacts: only allow radii matching known cylinders
                if round(r, 1) not in known_cyl_radii:
                    exp2.Next()
                    continue
                # Skip partial arcs (fillet/blend edges) — only keep full circles
                edge_span = abs(curve.LastParameter() - curve.FirstParameter())
                if edge_span < _math.pi * 1.5:  # less than ~270° → fillet edge
                    exp2.Next()
                    continue
                key = (round(loc.X(), 1), round(loc.Y(), 1), round(loc.Z(), 1), round(r, 1))
                if key not in seen_circles and key not in seen_cyls:
                    seen_circles.add(key)
                    ax_dir = circ.Axis().Direction()
                    adx, ady, adz = ax_dir.X(), ax_dir.Y(), ax_dir.Z()
                    # Deduplicate by projected position
                    if abs(adz) > 0.5:
                        proj_key = (round(loc.X(), 0), round(loc.Y(), 0), round(r, 0))
                    elif abs(adx) > 0.5:
                        proj_key = (round(loc.Y(), 0), round(loc.Z(), 0), round(r, 0))
                    else:
                        proj_key = (round(loc.X(), 0), round(loc.Z(), 0), round(r, 0))
                    if proj_key not in seen_cyls_proj:
                        seen_cyls_proj.add(proj_key)
                        features.circles.append(CircularFeature(
                            center=(loc.X(), loc.Y(), loc.Z()),
                            radius=r,
                            axis=(adx, ady, adz),
                            is_hole=True,
                        ))
            exp2.Next()

        # Store fillet info
        from svg.dimensions import FilletFeature
        for r in fillet_radii:
            features.fillets.append(FilletFeature(radius=r, center=(0, 0, 0)))

        # Extract pocket and step features from planar faces
        self._extract_detail_features(shape, features)

        return features

    def _extract_detail_features(self, shape, features: ModelFeatures) -> None:
        """Extract pockets and steps from planar faces using OCC topology."""
        try:
            from OCP.TopAbs import TopAbs_FACE
            from OCP.TopExp import TopExp_Explorer
            from OCP.GeomAbs import GeomAbs_Plane
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            from OCP.Bnd import Bnd_Box
            from OCP.BRepBndLib import BRepBndLib
            from OCP.TopoDS import TopoDS
        except ImportError:
            return

        xmin, ymin, zmin, xmax, ymax, zmax = features.bbox
        bbox_w = xmax - xmin
        bbox_d = ymax - ymin
        bbox_h = zmax - zmin

        if bbox_w < 1e-6 or bbox_d < 1e-6 or bbox_h < 1e-6:
            return

        tol = 0.5  # tolerance for boundary checks

        seen_pockets = set()
        seen_steps = set()

        exp = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
        while exp.More():
            face = TopoDS.Face_s(exp.Current())
            surf = BRepAdaptor_Surface(face)

            if surf.GetType() == GeomAbs_Plane:
                plane = surf.Plane()
                normal = plane.Axis().Direction()
                loc = plane.Location()

                nx, ny, nz = normal.X(), normal.Y(), normal.Z()

                # Get face bounding box
                face_box = Bnd_Box()
                BRepBndLib.Add_s(face, face_box)
                fx1, fy1, fz1, fx2, fy2, fz2 = face_box.Get()

                fw = fx2 - fx1
                fd = fy2 - fy1
                fh = fz2 - fz1

                # ── Detect horizontal planar faces (normal along Z) at intermediate Z ──
                if abs(nz) > 0.9:
                    face_z = loc.Z()

                    # Skip top/bottom faces of the model
                    if abs(face_z - zmax) < tol or abs(face_z - zmin) < tol:
                        exp.Next()
                        continue

                    # Skip very small faces (e.g. fillet flats)
                    if fw < 2 or fd < 2:
                        exp.Next()
                        continue

                    face_cx = (fx1 + fx2) / 2
                    face_cy = (fy1 + fy2) / 2

                    # Check if this is a pocket floor (enclosed inside model)
                    touches_x = (fx1 - xmin < tol) or (xmax - fx2 < tol)
                    touches_y = (fy1 - ymin < tol) or (ymax - fy2 < tol)

                    key = (round(face_cx, 0), round(face_cy, 0), round(face_z, 0))

                    if not touches_x and not touches_y:
                        # Enclosed pocket — use Z-level as key to merge split floors
                        z_key = round(face_z, 0)
                        if key not in seen_pockets:
                            seen_pockets.add(key)
                            depth = zmax - face_z if nz > 0 else face_z - zmin
                            if depth > 0.5 and fw * fd > 10:
                                # Skip faces that are actually circular hole floors:
                                # if width ≈ length and a circular feature exists nearby
                                # with diameter close to the face size, it's a hole floor.
                                _is_hole_floor = False
                                if fw > 0 and abs(fw - fd) / max(fw, fd) < 0.3:
                                    for cf in features.circles:
                                        cd = cf.radius * 2
                                        if abs(cd - max(fw, fd)) < 3:
                                            cx, cy, cz = cf.center
                                            if (abs(cx - face_cx) < cd and
                                                abs(cy - face_cy) < cd):
                                                _is_hole_floor = True
                                                break
                                if _is_hole_floor:
                                    exp.Next()
                                    continue
                                # Check if there's already a pocket at same Z-level nearby
                                merged = False
                                for p in features.pockets:
                                    if abs(p.normal[2]) > 0.5 and abs(p.center[2] - face_z) < 1:
                                        gap_x = abs(p.center[0] - face_cx) - (p.width + fw) / 2
                                        gap_y = abs(p.center[1] - face_cy) - (p.length + fd) / 2
                                        if gap_x < max(fw, p.width) and gap_y < 2:
                                            # Merge: expand pocket to encompass both
                                            new_x1 = min(p.center[0] - p.width/2, fx1)
                                            new_x2 = max(p.center[0] + p.width/2, fx2)
                                            new_y1 = min(p.center[1] - p.length/2, fy1)
                                            new_y2 = max(p.center[1] + p.length/2, fy2)
                                            p.width = new_x2 - new_x1
                                            p.length = new_y2 - new_y1
                                            p.center = ((new_x1+new_x2)/2, (new_y1+new_y2)/2, face_z)
                                            merged = True
                                            break
                                if not merged:
                                    features.pockets.append(PocketFeature(
                                        center=(face_cx, face_cy, face_z),
                                        width=fw,
                                        length=fd,
                                        depth=depth,
                                        normal=(nx, ny, nz),
                                    ))
                    elif touches_x != touches_y or (touches_x and touches_y):
                        # Step/shoulder: touches model boundary on at least one side
                        if key not in seen_steps:
                            seen_steps.add(key)
                            height = face_z - zmin if nz > 0 else zmax - face_z
                            if height > 0.5 and height < bbox_h - 0.5:
                                features.steps.append(StepFeature(
                                    corner=(face_cx, face_cy, face_z),
                                    width=fw,
                                    height=height,
                                    depth=fd,
                                    normal=(nx, ny, nz),
                                ))

                # ── Detect vertical planar faces (normal along X or Y) at intermediate positions ──
                # Only detect faces that represent a pocket wall opening to the exterior
                # (i.e. are large enough to be a real feature, not just an internal wall face)
                elif abs(nx) > 0.9:
                    face_x = loc.X()
                    if abs(face_x - xmax) < tol or abs(face_x - xmin) < tol:
                        exp.Next()
                        continue
                    # Must be a substantial face (not a thin pocket wall)
                    face_area = fd * fh
                    if fd < 3 or fh < 3 or face_area < bbox_d * bbox_h * 0.1:
                        exp.Next()
                        continue

                    face_cy = (fy1 + fy2) / 2
                    face_cz = (fz1 + fz2) / 2
                    key = (round(face_x, 0), round(face_cy, 0), round(face_cz, 0))

                    depth = xmax - face_x if nx > 0 else face_x - xmin
                    if depth > 1.0 and key not in seen_pockets:
                        seen_pockets.add(key)
                        features.pockets.append(PocketFeature(
                            center=(face_x, face_cy, face_cz),
                            width=fd,
                            length=fh,
                            depth=depth,
                            normal=(nx, ny, nz),
                        ))

                elif abs(ny) > 0.9:
                    face_y = loc.Y()
                    if abs(face_y - ymax) < tol or abs(face_y - ymin) < tol:
                        exp.Next()
                        continue
                    face_area = fw * fh
                    if fw < 3 or fh < 3 or face_area < bbox_w * bbox_h * 0.1:
                        exp.Next()
                        continue

                    face_cx = (fx1 + fx2) / 2
                    face_cz = (fz1 + fz2) / 2
                    key = (round(face_cx, 0), round(face_y, 0), round(face_cz, 0))

                    depth = ymax - face_y if ny > 0 else face_y - ymin
                    if depth > 1.0 and key not in seen_pockets:
                        seen_pockets.add(key)
                        features.pockets.append(PocketFeature(
                            center=(face_cx, face_y, face_cz),
                            width=fw,
                            length=fh,
                            depth=depth,
                            normal=(nx, ny, nz),
                        ))

            exp.Next()

    def _calculate_scale(self, dimensions: Tuple[float, float, float]) -> float:
        """Calculate optimal scale for the model.

        Args:
            dimensions: (width, height, depth)

        Returns:
            Scale factor
        """
        # Calculate bounding box in projection space
        # For all 4 views, we need to fit the largest dimension
        max_dim = max(dimensions)

        # Available space per view (2x2 grid)
        available = min(self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT)

        # Calculate scale with margin
        scale = available / (max_dim * 1.2)

        # Round to standard scale
        calc = ScaleCalculator(self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT)
        return calc.get_standard_scale(scale, prefer_larger=True)

    def export_png(self, svg_path: str, png_path: str, min_width: int = 2048) -> None:
        """Convert SVG drawing to high-resolution PNG.

        Args:
            svg_path: Path to SVG file to convert.
            png_path: Output PNG file path.
            min_width: Minimum width in pixels (default 2048 for 2K).
        """
        import cairosvg

        svg_p = Path(svg_path)
        if not svg_p.exists():
            raise FileNotFoundError(f"SVG not found: {svg_path}")

        # Read SVG to determine aspect ratio
        tree = ET.parse(str(svg_p))
        root = tree.getroot()
        vb = root.get("viewBox", "")
        if vb:
            parts = vb.split()
            svg_w, svg_h = float(parts[2]), float(parts[3])
        else:
            svg_w = float(root.get("width", "800").replace("mm", "").strip())
            svg_h = float(root.get("height", "600").replace("mm", "").strip())

        aspect = svg_w / svg_h if svg_h > 0 else 1.0
        out_width = max(min_width, 2048)
        out_height = int(out_width / aspect)

        cairosvg.svg2png(
            url=str(svg_p),
            write_to=png_path,
            output_width=out_width,
            output_height=out_height,
            background_color="white",
        )
        print(f"PNG written to: {png_path}")

    def export_rendered_png(self, png_path: str, width: int = 2048, height: int = 1536) -> None:
        """Export a shaded 3D render of the model as PNG (no line art).

        Uses VTK off-screen rendering with a clean studio-style lighting setup.

        Args:
            png_path: Output PNG file path.
            width: Image width in pixels.
            height: Image height in pixels.
        """
        if self.model is None:
            raise ValueError("No model loaded.")

        import vtk
        from OCP.StlAPI import StlAPI_Writer
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        import tempfile, os

        shape = self.model.val() if isinstance(self.model, cadquery.Workplane) else self.model

        # Tessellate to STL via temp file
        tmp_stl = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
        tmp_stl.close()
        try:
            mesh = BRepMesh_IncrementalMesh(shape.wrapped, 0.1, False, 0.5, True)
            mesh.Perform()
            writer = StlAPI_Writer()
            writer.Write(shape.wrapped, tmp_stl.name)

            # VTK pipeline
            reader = vtk.vtkSTLReader()
            reader.SetFileName(tmp_stl.name)
            reader.Update()

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(reader.GetOutputPort())

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            # Steel-blue material appearance
            actor.GetProperty().SetColor(0.65, 0.72, 0.78)
            actor.GetProperty().SetSpecular(0.4)
            actor.GetProperty().SetSpecularPower(30)
            actor.GetProperty().SetDiffuse(0.7)
            actor.GetProperty().SetAmbient(0.3)
            actor.GetProperty().SetInterpolationToPhong()

            renderer = vtk.vtkRenderer()
            renderer.AddActor(actor)
            renderer.SetBackground(0.95, 0.95, 0.97)  # Light grey background

            # Studio lighting
            renderer.RemoveAllLights()
            key_light = vtk.vtkLight()
            key_light.SetPosition(1, 1, 1)
            key_light.SetFocalPoint(0, 0, 0)
            key_light.SetIntensity(0.8)
            key_light.SetColor(1.0, 1.0, 1.0)
            renderer.AddLight(key_light)

            fill_light = vtk.vtkLight()
            fill_light.SetPosition(-1, 0.5, 0.5)
            fill_light.SetFocalPoint(0, 0, 0)
            fill_light.SetIntensity(0.4)
            fill_light.SetColor(0.9, 0.95, 1.0)
            renderer.AddLight(fill_light)

            back_light = vtk.vtkLight()
            back_light.SetPosition(0, -1, 1)
            back_light.SetFocalPoint(0, 0, 0)
            back_light.SetIntensity(0.3)
            renderer.AddLight(back_light)

            # Camera — isometric-like angle
            camera = renderer.GetActiveCamera()
            bounds = actor.GetBounds()
            cx = (bounds[0] + bounds[1]) / 2
            cy = (bounds[2] + bounds[3]) / 2
            cz = (bounds[4] + bounds[5]) / 2
            diag = ((bounds[1]-bounds[0])**2 + (bounds[3]-bounds[2])**2 + (bounds[5]-bounds[4])**2) ** 0.5
            dist = diag * 2.0
            camera.SetFocalPoint(cx, cy, cz)
            camera.SetPosition(cx + dist * 0.6, cy - dist * 0.7, cz + dist * 0.5)
            camera.SetViewUp(0, 0, 1)
            renderer.ResetCamera()
            camera.Dolly(1.3)
            renderer.ResetCameraClippingRange()

            # Off-screen render
            render_window = vtk.vtkRenderWindow()
            render_window.SetOffScreenRendering(1)
            render_window.SetSize(width, height)
            render_window.AddRenderer(renderer)
            render_window.Render()

            # Write PNG
            w2i = vtk.vtkWindowToImageFilter()
            w2i.SetInput(render_window)
            w2i.SetScale(1)
            w2i.SetInputBufferTypeToRGBA()
            w2i.ReadFrontBufferOff()
            w2i.Update()

            png_writer = vtk.vtkPNGWriter()
            png_writer.SetFileName(png_path)
            png_writer.SetInputConnection(w2i.GetOutputPort())
            png_writer.Write()

            print(f"Rendered PNG written to: {png_path}")
        finally:
            os.unlink(tmp_stl.name)

    def export_step(self, output_path: str) -> None:
        """Export model to STEP file for 3D viewing.

        Args:
            output_path: Output STEP file path (.stp or .step)
        """
        if self.model is None:
            raise ValueError("No model loaded. Call load_model() or load_model_from_file() first.")

        try:
            # Get the underlying OCC shape(s)
            if isinstance(self.model, cadquery.Workplane):
                # For Workplane, get all solids and export them
                solids = self.model.solids()
                if len(solids.objects) == 1:
                    # Single solid - export directly
                    self.model.val().exportStep(output_path)
                else:
                    # Multiple solids - export using first solid
                    self.model.val().exportStep(output_path)
            elif isinstance(self.model, cadquery.Solid):
                self.model.exportStep(output_path)
            else:
                raise ValueError(f"Unsupported model type: {type(self.model)}")

            print(f"STEP file written to: {output_path}")
        except Exception as e:
            raise RuntimeError(f"Failed to export STEP file: {e}")

    def export_all(
        self,
        svg_output_path: str,
        step_output_path: Optional[str] = None,
        png_output_path: Optional[str] = None,
    ) -> dict:
        """Export annotated SVG, STEP, and drawing PNG outputs in one call.

        Args:
            svg_output_path: Output SVG file path.
            step_output_path: Output STEP file path. If omitted, derives from SVG path.
            png_output_path: Output PNG (drawing) path. If omitted, derives from SVG path.
        Returns:
            Dict with keys: step_path, png_path.
        """
        svg_path = Path(svg_output_path)
        if svg_path.suffix.lower() != ".svg":
            svg_path = svg_path.with_suffix(".svg")

        if step_output_path:
            step_path = Path(step_output_path)
        else:
            step_path = svg_path.with_suffix(".stp")

        if step_path.suffix.lower() not in (".stp", ".step"):
            step_path = step_path.with_suffix(".stp")

        png_path = Path(png_output_path) if png_output_path else svg_path.with_suffix(".png")

        self.render(str(svg_path))
        self.export_step(str(step_path))
        self.export_png(str(svg_path), str(png_path))

        return {
            "step_path": str(step_path),
            "png_path": str(png_path),
        }

    def render(self, output_path: str) -> None:
        """Render model to A3 SVG with four views using CadQuery native exporter.

        Args:
            output_path: Output SVG file path
        """
        if self.model is None:
            raise ValueError("No model loaded. Call load_model() or load_model_from_file() first.")

        svg_path = Path(output_path)
        if svg_path.suffix.lower() != ".svg":
            svg_path = svg_path.with_suffix(".svg")

        shape = self.model.val() if isinstance(self.model, cadquery.Workplane) else self.model
        svg = SVGWriter(self.layout.WIDTH, self.layout.HEIGHT, "mm")

        # Get model 3D bounding box and features for dimension annotations
        dimensions = self._analyze_model()
        dim_x, dim_y, dim_z = dimensions  # width(X), depth(Y), height(Z)
        model_features = self._extract_features()

        # Four views generated by CadQuery native HLR SVG.
        views = [
            ("Isometric", self.layout.VIEW_POSITIONS[0], self.view_projection_dirs["isometric"], 0.0),
            ("Top", self.layout.VIEW_POSITIONS[1], self.view_projection_dirs["top"], 0.0),
            ("Front", self.layout.VIEW_POSITIONS[2], self.view_projection_dirs["front"], 0.0),
            ("Right", self.layout.VIEW_POSITIONS[3], self.view_projection_dirs["right"], 0.0),
        ]

        rendered_views = []
        for label, pos, proj, rotate_deg in views:
            native_svg = self._get_svg_with_complete_hidden(
                shape,
                {
                    "width": self.layout.VIEW_WIDTH,
                    "height": self.layout.VIEW_HEIGHT,
                    "marginLeft": self.layout.VIEW_INNER_MARGIN,
                    "marginTop": self.layout.VIEW_INNER_MARGIN,
                    "projectionDir": proj,
                    "showAxes": False,
                    "showHidden": True,
                    "strokeColor": (0, 0, 139),
                    "hiddenColor": (130, 130, 130),
                    "strokeWidth": 0.5,
                },
            )
            base_scale = self._extract_native_scale(native_svg)
            rendered_views.append((label, pos, rotate_deg, native_svg, base_scale))

        valid_scales = [v[4] for v in rendered_views if v[4] > 1e-9]
        common_scale = min(valid_scales) if valid_scales else 1.0
        # Fill-first auto scale: keep a little headroom to avoid touching borders.
        self.scale = common_scale * self.layout.SCALE_HEADROOM

        # Collect model bounds per view for dimension annotations
        view_bounds: dict[str, Tuple[Tuple[float, float], ModelBounds]] = {}

        for label, (x, y), rotate_deg, native_svg, base_scale in rendered_views:
            inner = self._extract_svg_inner(native_svg)
            scale_ratio = self.scale / base_scale if base_scale > 1e-9 else 1.0
            inner = self._enhance_hidden_line_style(inner, base_scale, scale_ratio)

            dx, dy, m_bounds = self._compute_native_center_offset(
                native_svg,
                self.layout.VIEW_WIDTH,
                self.layout.VIEW_HEIGHT,
                scale_ratio,
            )
            if m_bounds is not None:
                view_bounds[label] = ((x, y), m_bounds)

            centered_inner = (
                f'<g transform="translate({dx},{dy})">'
                f'<g transform="scale({scale_ratio})">{inner}</g>'
                f'</g>'
            )

            svg.add_viewport(x, y, self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT)
            if rotate_deg:
                cx = self.layout.VIEW_WIDTH / 2
                cy = self.layout.VIEW_HEIGHT / 2
                svg.add_raw(
                    f'<g transform="translate({cx},{cy}) rotate({rotate_deg}) translate({-cx},{-cy})">{centered_inner}</g>'
                )
            else:
                svg.add_raw(centered_inner)
            svg.end_viewport()

            svg.add_text(
                x + self.layout.VIEW_WIDTH / 2,
                y + self.layout.VIEW_HEIGHT - 3,
                label,
                font_size=3.5,
                font_family="Arial, sans-serif",
            )
            svg.add_rect(
                x,
                y,
                self.layout.VIEW_WIDTH,
                self.layout.VIEW_HEIGHT,
                stroke="#808080",
                stroke_width=0.5,
            )

        # Add dimension annotations to orthographic views.
        # A shared set tracks what has been annotated so each dimension
        # appears in exactly one view (standard engineering drawing practice).
        dim_annotator = DimensionAnnotator(svg)
        annotated = set()  # shared across all three views
        # Top view: horizontal = Width(X), vertical = Depth(Y)
        if "Top" in view_bounds:
            (vx, vy), bounds = view_bounds["Top"]
            dim_annotator.annotate_view(
                "Top", vx, vy,
                self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT,
                bounds, dim_x, dim_y, model_features,
                dim_h_axis="X", dim_v_axis="Y",
                annotated_dims=annotated,
            )
        # Front view: horizontal = Width(X), vertical = Height(Z)
        if "Front" in view_bounds:
            (vx, vy), bounds = view_bounds["Front"]
            dim_annotator.annotate_view(
                "Front", vx, vy,
                self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT,
                bounds, dim_x, dim_z, model_features,
                dim_h_axis="X", dim_v_axis="Z",
                annotated_dims=annotated,
            )
        # Right view: horizontal = Depth(Y), vertical = Height(Z)
        if "Right" in view_bounds:
            (vx, vy), bounds = view_bounds["Right"]
            dim_annotator.annotate_view(
                "Right", vx, vy,
                self.layout.VIEW_WIDTH, self.layout.VIEW_HEIGHT,
                bounds, dim_y, dim_z, model_features,
                dim_h_axis="Y", dim_v_axis="Z",
                annotated_dims=annotated,
            )

        self._add_scale_annotation(svg)
        self._add_title_block(svg)
        svg.write(str(svg_path))
        print(f"SVG written to: {svg_path}")

    def _get_svg_with_complete_hidden(self, shape, opts: dict) -> str:
        """CadQuery native SVG export with hidden smooth lines included."""
        if cq_svg_export is None:
            return cadquery.exporters.getSVG(shape, opts=opts)

        d = {
            "width": 800,
            "height": 240,
            "marginLeft": 200,
            "marginTop": 20,
            "projectionDir": (-1.75, 1.1, 5),
            "showAxes": True,
            "strokeWidth": -1.0,
            "strokeColor": (0, 0, 139),
            "hiddenColor": (160, 160, 160),
            "showHidden": True,
            "focus": None,
        }
        d.update(opts or {})

        width = float(d["width"]) if d["width"] is not None else None
        height = float(d["height"]) if d["height"] is not None else None
        margin_left = float(d["marginLeft"])
        margin_top = float(d["marginTop"])
        projection_dir = tuple(d["projectionDir"])
        show_axes = bool(d["showAxes"])
        stroke_width = float(d["strokeWidth"])
        stroke_color = tuple(d["strokeColor"])
        hidden_color = tuple(d["hiddenColor"])
        show_hidden = bool(d["showHidden"])
        focus = float(d["focus"]) if d.get("focus") else None

        hlr = HLRBRep_Algo()
        hlr.Add(shape.wrapped)
        cs = self._build_projection_cs(projection_dir)
        projector = HLRAlgo_Projector(cs, focus) if focus is not None else HLRAlgo_Projector(cs)
        hlr.Projector(projector)
        hlr.Update()
        hlr.Hide()
        hlr_shapes = HLRBRep_HLRToShape(hlr)

        visible = []
        v_sharp = hlr_shapes.VCompound()
        if not v_sharp.IsNull():
            visible.append(v_sharp)
        v_smooth = hlr_shapes.Rg1LineVCompound()
        if not v_smooth.IsNull():
            visible.append(v_smooth)
        v_outline = hlr_shapes.OutLineVCompound()
        if not v_outline.IsNull():
            visible.append(v_outline)

        hidden = []
        h_sharp = hlr_shapes.HCompound()
        if not h_sharp.IsNull():
            hidden.append(h_sharp)
        h_outline = hlr_shapes.OutLineHCompound()
        if not h_outline.IsNull():
            hidden.append(h_outline)
        # Key fix: include hidden smooth lines that CadQuery default exporter omits.
        h_smooth = hlr_shapes.Rg1LineHCompound()
        if not h_smooth.IsNull():
            hidden.append(h_smooth)

        for el in visible:
            cq_svg_export.BRepLib.BuildCurves3d_s(el, cq_svg_export.TOLERANCE)
        for el in hidden:
            cq_svg_export.BRepLib.BuildCurves3d_s(el, cq_svg_export.TOLERANCE)

        visible = list(map(cq_svg_export.Shape, visible))
        hidden = list(map(cq_svg_export.Shape, hidden))
        hidden_paths, visible_paths = cq_svg_export.getPaths(visible, hidden)
        bb = cq_svg_export.Compound.makeCompound(hidden + visible).BoundingBox()

        if width is None or height is None:
            if width is None:
                width = (height - (2.0 * margin_top)) * (bb.xlen / bb.ylen) + 2.0 * margin_left
            else:
                height = (width - 2.0 * margin_left) * (bb.ylen / bb.xlen) + 2.0 * margin_top
            unit_scale = (width - 2.0 * margin_left) / bb.xlen
        else:
            bb_scale = self.layout.VIEW_FIT_RATIO
            unit_scale = min(width / bb.xlen * bb_scale, height / bb.ylen * bb_scale)

        x_translate = (0 - bb.xmin) + margin_left / unit_scale
        y_translate = (0 - bb.ymax) - margin_top / unit_scale
        if stroke_width == -1.0:
            stroke_width = 1.0 / unit_scale

        hidden_content = ""
        if show_hidden:
            for p in hidden_paths:
                hidden_content += cq_svg_export.PATHTEMPLATE % p
        visible_content = ""
        for p in visible_paths:
            visible_content += cq_svg_export.PATHTEMPLATE % p

        if show_axes and projection_dir == (-1.75, 1.1, 5):
            axes = cq_svg_export.AXES_TEMPLATE % (
                {"unitScale": str(unit_scale), "textboxY": str(height - 30), "uom": str(cq_svg_export.guessUnitOfMeasure(shape))}
            )
        else:
            axes = ""

        return cq_svg_export.SVG_TEMPLATE % (
            {
                "unitScale": str(unit_scale),
                "strokeWidth": str(stroke_width),
                "strokeColor": ",".join([str(x) for x in stroke_color]),
                "hiddenColor": ",".join([str(x) for x in hidden_color]),
                "hiddenContent": hidden_content,
                "visibleContent": visible_content,
                "xTranslate": str(x_translate),
                "yTranslate": str(y_translate),
                "width": str(width),
                "height": str(height),
                "textboxY": str(height - 30),
                "uom": str(cq_svg_export.guessUnitOfMeasure(shape)),
                "axesIndicator": axes,
            }
        )

    def _build_projection_cs(self, projection_dir: Tuple[float, float, float]) -> gp_Ax2:
        """Build a stable projector coordinate system with Z-up screen orientation.

        CadQuery/OCC only receiving view direction can pick an arbitrary in-plane roll.
        We explicitly define X direction so projected world Z tends to map upward.
        """
        vx, vy, vz = projection_dir
        norm = math.sqrt(vx * vx + vy * vy + vz * vz)
        if norm < 1e-12:
            return gp_Ax2(gp_Pnt(), gp_Dir(0.0, 0.0, 1.0))

        vx /= norm
        vy /= norm
        vz /= norm

        # Preferred screen-up comes from projecting world Z onto the view plane.
        upx, upy, upz = 0.0, 0.0, 1.0
        dot_up = upx * vx + upy * vy + upz * vz
        upx -= dot_up * vx
        upy -= dot_up * vy
        upz -= dot_up * vz

        up_norm = math.sqrt(upx * upx + upy * upy + upz * upz)
        if up_norm < 1e-9:
            # Fallback when view direction is parallel to world Z.
            upx, upy, upz = 0.0, 1.0, 0.0
            dot_up = upx * vx + upy * vy + upz * vz
            upx -= dot_up * vx
            upy -= dot_up * vy
            upz -= dot_up * vz
            up_norm = math.sqrt(upx * upx + upy * upy + upz * upz)
            if up_norm < 1e-12:
                return gp_Ax2(gp_Pnt(), gp_Dir(vx, vy, vz))

        upx /= up_norm
        upy /= up_norm
        upz /= up_norm

        # OCC Ax2 defines Y as Direction x XDirection, so choose X = Up x Direction.
        xdx = upy * vz - upz * vy
        xdy = upz * vx - upx * vz
        xdz = upx * vy - upy * vx
        xd_norm = math.sqrt(xdx * xdx + xdy * xdy + xdz * xdz)
        if xd_norm < 1e-12:
            return gp_Ax2(gp_Pnt(), gp_Dir(vx, vy, vz))

        xdx /= xd_norm
        xdy /= xd_norm
        xdz /= xd_norm

        return gp_Ax2(gp_Pnt(), gp_Dir(vx, vy, vz), gp_Dir(xdx, xdy, xdz))

    def _extract_svg_inner(self, svg_text: str) -> str:
        """Extract inner content from a CadQuery-generated SVG text."""
        without_prefix = re.sub(r"(?is)^.*?<svg[^>]*>", "", svg_text, count=1)
        inner = re.sub(r"(?is)</svg>\s*$", "", without_prefix, count=1)
        return inner.strip()

    def _extract_native_scale(self, svg_text: str) -> float:
        """Extract primary scale factor from CadQuery-generated SVG group transform."""
        transform_match = re.search(
            r'transform="scale\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)\s*translate\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)"',
            svg_text,
        )
        if not transform_match:
            return 1.0
        return abs(float(transform_match.group(1)))

    def _enhance_hidden_line_style(
        self, svg_inner: str, unit_scale: float = 1.0, scale_ratio: float = 1.0
    ) -> str:
        """Improve visibility of hidden lines and remove overlapping hidden/visible edges.

        OCC HLR outputs back-face edges as hidden AND front-face edges as visible
        at the same projected position.  Instead of relying solely on a visual
        knockout layer, we:
        1. Parse visible path segments into a spatial index.
        2. Remove hidden paths whose segments duplicate or lie on visible paths.
        3. Remove duplicate hidden paths (same edge from multiple faces).
        4. Draw a white knockout layer as safety net.
        5. Draw visible paths on top.
        """
        effective_scale = unit_scale * scale_ratio
        if effective_scale < 1e-9:
            effective_scale = 1.0

        dash_val = 2.0 / effective_scale
        gap_val = 1.0 / effective_scale
        vis_sw = 0.7 / effective_scale
        hid_sw = 0.25 / effective_scale
        knockout_sw = 1.2 / effective_scale

        # ---- extract visible and hidden groups ----
        vis_group_match = re.search(
            r'(<g\s+stroke="rgb\(0,0,139\)"\s+fill="none")([^>]*>)(.*?)(</g>)',
            svg_inner,
            re.DOTALL,
        )
        hid_group_match = re.search(
            r'(<g\s+stroke="rgb\([^"]+\)"\s+fill="none"\s+stroke-dasharray="[^"]+")'
            r'([^>]*>)(.*?)(</g>)',
            svg_inner,
            re.DOTALL,
        )

        if vis_group_match and hid_group_match:
            vis_paths_str = vis_group_match.group(3)
            hid_paths_str = hid_group_match.group(3)

            # Build a set of normalised visible line segments for fast lookup.
            vis_segments = self._extract_path_segments(vis_paths_str)

            # Filter hidden paths: drop those that overlap visible segments.
            filtered_hidden = self._filter_hidden_paths(hid_paths_str, vis_segments)

            # Reconstruct hidden group with filtered content
            new_hid_group = (
                hid_group_match.group(1)
                + f' stroke-width="{hid_sw:.4f}">'
                + filtered_hidden
                + hid_group_match.group(4)
            )

            # Reconstruct visible group with stroke-width
            new_vis_group = (
                vis_group_match.group(1)
                + f' stroke-width="{vis_sw:.4f}">'
                + vis_group_match.group(3)
                + vis_group_match.group(4)
            )

            # Knockout group
            knockout_group = (
                f'<g stroke="white" fill="none" stroke-width="{knockout_sw:.4f}">'
                + vis_group_match.group(3)
                + '</g>\n'
            )

            # Update dasharray value
            new_hid_group = re.sub(
                r'stroke-dasharray="[^"]+"',
                f'stroke-dasharray="{dash_val:.4f},{gap_val:.4f}"',
                new_hid_group,
            )

            # Rebuild SVG: hidden -> knockout -> visible
            # Remove old groups and rebuild
            before_hid = svg_inner[:hid_group_match.start()]
            between = svg_inner[hid_group_match.end():vis_group_match.start()]
            after_vis = svg_inner[vis_group_match.end():]

            updated = before_hid + new_hid_group + between + knockout_group + new_vis_group + after_vis
        else:
            # Fallback: apply basic stroke styling without dedup
            updated = re.sub(
                r'stroke-dasharray="[^"]+"',
                f'stroke-dasharray="{dash_val:.4f},{gap_val:.4f}"',
                svg_inner,
            )
            updated = re.sub(
                r'(<g\s+stroke="rgb\([^"]+\)"\s+fill="none"\s+stroke-dasharray="[^"]+")',
                rf'\1 stroke-width="{hid_sw:.4f}"',
                updated,
            )
            updated = re.sub(
                r'(<g\s+stroke="rgb\(0,0,139\)"\s+fill="none")(?!\s+stroke-dasharray)',
                rf'\1 stroke-width="{vis_sw:.4f}"',
                updated,
            )

        return updated

    @staticmethod
    def _extract_path_segments(paths_svg: str) -> set:
        """Extract normalised line segments from SVG path data.

        Returns a set of ((x1,y1),(x2,y2)) tuples with coordinates rounded
        to 2 decimal places and endpoints sorted for order-independence.
        """
        segments = set()
        for d in re.findall(r'd="([^"]+)"', paths_svg):
            pts = re.findall(r'(-?[\d.]+(?:[eE][+-]?\d+)?)\s*,\s*(-?[\d.]+(?:[eE][+-]?\d+)?)', d)
            for i in range(len(pts) - 1):
                p1 = (round(float(pts[i][0]), 2), round(float(pts[i][1]), 2))
                p2 = (round(float(pts[i + 1][0]), 2), round(float(pts[i + 1][1]), 2))
                seg = tuple(sorted([p1, p2]))
                segments.add(seg)
        return segments

    @staticmethod
    def _path_overlaps_visible(path_d: str, vis_segments: set, tolerance: float = 0.5) -> bool:
        """Check if a hidden path overlaps with any visible segment.

        A hidden path overlaps if ALL of its points lie on (or very close to)
        a visible line segment.
        """
        pts = re.findall(r'(-?[\d.]+(?:[eE][+-]?\d+)?)\s*,\s*(-?[\d.]+(?:[eE][+-]?\d+)?)', path_d)
        if len(pts) < 2:
            return False

        fpts = [(round(float(p[0]), 2), round(float(p[1]), 2)) for p in pts]

        # Check each sub-segment of the hidden path
        for i in range(len(fpts) - 1):
            p1, p2 = fpts[i], fpts[i + 1]
            seg = tuple(sorted([p1, p2]))
            # Exact match
            if seg in vis_segments:
                continue
            # Check if this sub-segment lies ON a visible segment (collinear + contained)
            if not CadQueryToSVG._segment_on_any_visible(p1, p2, vis_segments, tolerance):
                return False
        return True

    @staticmethod
    def _segment_on_any_visible(p1, p2, vis_segments: set, tol: float) -> bool:
        """Check if segment p1-p2 lies on any visible segment."""
        for (v1, v2) in vis_segments:
            if CadQueryToSVG._segment_on_segment(p1, p2, v1, v2, tol):
                return True
        return False

    @staticmethod
    def _segment_on_segment(p1, p2, v1, v2, tol: float) -> bool:
        """Check if segment p1-p2 lies entirely on segment v1-v2."""
        # Both points must be close to the line defined by v1-v2
        dx, dy = v2[0] - v1[0], v2[1] - v1[1]
        seg_len = (dx * dx + dy * dy) ** 0.5
        if seg_len < 1e-9:
            # Degenerate visible segment - check point distance
            d1 = ((p1[0] - v1[0]) ** 2 + (p1[1] - v1[1]) ** 2) ** 0.5
            d2 = ((p2[0] - v1[0]) ** 2 + (p2[1] - v1[1]) ** 2) ** 0.5
            return d1 < tol and d2 < tol

        # Distance from point to line
        for pt in [p1, p2]:
            cross = abs(dx * (v1[1] - pt[1]) - dy * (v1[0] - pt[0]))
            dist = cross / seg_len
            if dist > tol:
                return False

        # Check that both points project within the segment extent (with tolerance)
        for pt in [p1, p2]:
            t = ((pt[0] - v1[0]) * dx + (pt[1] - v1[1]) * dy) / (seg_len * seg_len)
            if t < -tol / seg_len or t > 1.0 + tol / seg_len:
                return False

        return True

    @staticmethod
    def _filter_hidden_paths(hidden_svg: str, vis_segments: set) -> str:
        """Remove hidden paths that duplicate visible edges, and deduplicate hidden paths."""
        seen_paths = set()
        result_parts = []
        # Split into path elements and non-path content
        parts = re.split(r'(<path\s+[^/]*/>)', hidden_svg)

        for part in parts:
            path_match = re.match(r'<path\s+[^>]*d="([^"]+)"[^/]*/>', part)
            if not path_match:
                result_parts.append(part)
                continue

            path_d = path_match.group(1)

            # Normalise for dedup: round coordinates and sort endpoints for simple lines
            norm_key = CadQueryToSVG._normalise_path_key(path_d)
            if norm_key in seen_paths:
                continue  # Skip duplicate hidden path
            seen_paths.add(norm_key)

            # Check if this hidden path overlaps a visible path
            if CadQueryToSVG._path_overlaps_visible(path_d, vis_segments):
                continue  # Skip hidden path that duplicates visible edge

            result_parts.append(part)

        return ''.join(result_parts)

    @staticmethod
    def _normalise_path_key(path_d: str) -> str:
        """Create a normalised key for a path for deduplication."""
        pts = re.findall(r'(-?[\d.]+(?:[eE][+-]?\d+)?)\s*,\s*(-?[\d.]+(?:[eE][+-]?\d+)?)', path_d)
        if not pts:
            return path_d
        rounded = [(round(float(x), 1), round(float(y), 1)) for x, y in pts]
        # Sort endpoints for simple 2-point paths (lines)
        if len(rounded) == 2:
            rounded = sorted(rounded)
        return str(rounded)

    def _compute_native_center_offset(
        self,
        svg_text: str,
        view_width: float,
        view_height: float,
        scale_ratio: float = 1.0,
    ) -> Tuple[float, float, Optional['ModelBounds']]:
        """Compute translation offset to center native CadQuery SVG in viewport.

        Returns:
            (dx, dy, model_bounds) where model_bounds is the bounding rect
            in viewport-local coordinates after centering, or None on failure.
        """
        transform_match = re.search(
            r'transform="scale\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)\s*translate\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)"',
            svg_text,
        )
        if not transform_match:
            return (0.0, 0.0, None)

        sx = float(transform_match.group(1))
        sy = float(transform_match.group(2))
        tx = float(transform_match.group(3))
        ty = float(transform_match.group(4))
        if abs(sx) < 1e-12 or abs(sy) < 1e-12:
            return (0.0, 0.0, None)

        points = []
        path_data = re.findall(r'<path\s+[^>]*d="([^"]+)"', svg_text)
        for d in path_data:
            points.extend(self._extract_svg_path_points(d))

        transformed_points = []
        for x, y in points:
            transformed_points.append((scale_ratio * sx * (x + tx), scale_ratio * sy * (y + ty)))

        if not transformed_points:
            return (0.0, 0.0, None)

        min_x = min(p[0] for p in transformed_points)
        max_x = max(p[0] for p in transformed_points)
        min_y = min(p[1] for p in transformed_points)
        max_y = max(p[1] for p in transformed_points)

        content_w = max_x - min_x
        content_h = max_y - min_y
        if content_w <= 0 or content_h <= 0:
            return (0.0, 0.0, None)

        dx = (view_width - content_w) / 2.0 - min_x
        dy = (view_height - content_h) / 2.0 - min_y
        if not (math.isfinite(dx) and math.isfinite(dy)):
            return (0.0, 0.0, None)

        # Model bounds in viewport-local coords after centering
        model_bounds = ModelBounds(
            left=min_x + dx,
            right=max_x + dx,
            top=min_y + dy,
            bottom=max_y + dy,
        )

        return (dx, dy, model_bounds)

    def _remove_overlapping_paths(self, svg_inner: str) -> str:
        """Remove overlapping path duplicates; prefer visible over hidden paths."""
        wrapped = f"<root>{svg_inner}</root>"
        try:
            root = ET.fromstring(wrapped)
        except ET.ParseError:
            return svg_inner

        # key -> (priority, order, parent, node); visible has higher priority than dashed.
        keep_map: dict[str, tuple[int, int, ET.Element, ET.Element]] = {}
        to_remove: list[tuple[ET.Element, ET.Element]] = []
        visible_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        hidden_nodes: list[tuple[ET.Element, ET.Element, str, bool]] = []
        order = 0

        def walk(node: ET.Element, parent: ET.Element | None, dashed_parent: bool) -> None:
            nonlocal order
            dashed_now = dashed_parent or ("stroke-dasharray" in node.attrib)
            if node.tag.endswith("path") and parent is not None:
                d = node.attrib.get("d", "")
                key = self._path_overlap_key(d)
                if key:
                    priority = 0 if dashed_now else 1
                    prev = keep_map.get(key)
                    if prev is None:
                        keep_map[key] = (priority, order, parent, node)
                    else:
                        prev_priority, prev_order, prev_parent, prev_node = prev
                        if priority > prev_priority:
                            to_remove.append((prev_parent, prev_node))
                            keep_map[key] = (priority, order, parent, node)
                        else:
                            to_remove.append((parent, node))

                is_linear = self._is_linear_path(d)
                if is_linear:
                    pts = self._extract_svg_path_points(d)
                    segs = self._segments_from_points(pts)
                    if not dashed_now:
                        visible_segments.extend(segs)
                    else:
                        hidden_nodes.append((parent, node, d, True))
                elif dashed_now:
                    hidden_nodes.append((parent, node, d, False))
                order += 1

            for child in list(node):
                walk(child, node, dashed_now)

        for child in list(root):
            walk(child, root, False)

        for parent, node in to_remove:
            try:
                parent.remove(node)
            except ValueError:
                pass

        # Remove local overlaps: clip hidden segments against visible segments.
        for parent, node, d, can_clip_locally in hidden_nodes:
            if node not in list(parent):
                continue
            if not can_clip_locally:
                continue
            pts = self._extract_svg_path_points(d)
            segs = self._segments_from_points(pts)
            clipped: list[tuple[tuple[float, float], tuple[float, float]]] = []
            for seg in segs:
                remaining = [seg]
                for vseg in visible_segments:
                    next_remaining: list[tuple[tuple[float, float], tuple[float, float]]] = []
                    for part in remaining:
                        next_remaining.extend(self._subtract_overlap_segment(part, vseg))
                    remaining = next_remaining
                    if not remaining:
                        break
                clipped.extend(remaining)

            if not clipped:
                try:
                    parent.remove(node)
                except ValueError:
                    pass
                continue

            # Deduplicate hidden segments after clipping.
            uniq: list[tuple[tuple[float, float], tuple[float, float]]] = []
            seen: set[str] = set()
            for s in clipped:
                key = self._segment_key(s)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(s)
            node.attrib["d"] = self._segments_to_path_d(uniq)

        return "".join(ET.tostring(child, encoding="unicode") for child in list(root))

    def _path_overlap_key(self, d: str) -> str:
        """Build a direction-invariant key from path geometry."""
        pts = self._extract_svg_path_points(d)
        if not pts:
            return re.sub(r"\s+", " ", d.strip())

        quantized = [(round(x, 3), round(y, 3)) for x, y in pts]
        if len(quantized) == 1:
            return f"{quantized[0][0]},{quantized[0][1]}"

        seq = ";".join(f"{x},{y}" for x, y in quantized)
        rev = ";".join(f"{x},{y}" for x, y in reversed(quantized))
        return min(seq, rev)

    def _is_linear_path(self, d: str) -> bool:
        """Return True when path uses only linear commands."""
        # Strip scientific-notation exponents (e.g. 1.5e-10) so 'e'/'E' is not
        # mistaken for an SVG command letter.
        stripped = re.sub(r"[eE][-+]?\d+", "", d)
        cmds = re.findall(r"[A-Za-z]", stripped)
        linear = {"M", "m", "L", "l", "H", "h", "V", "v", "Z", "z"}
        return all(c in linear for c in cmds)

    def _segments_from_points(
        self, pts: List[Tuple[float, float]]
    ) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        if len(pts) < 2:
            return segs
        for i in range(len(pts) - 1):
            p1 = pts[i]
            p2 = pts[i + 1]
            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < 1e-6:
                continue
            segs.append((p1, p2))
        return segs

    def _subtract_overlap_segment(
        self,
        base_seg: Tuple[Tuple[float, float], Tuple[float, float]],
        cover_seg: Tuple[Tuple[float, float], Tuple[float, float]],
    ) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
        """Subtract colinear overlap of cover_seg from base_seg."""
        interval = self._overlap_interval_on_base(base_seg, cover_seg)
        if interval is None:
            return [base_seg]

        t0, t1 = interval
        eps = 1e-6
        a, b = base_seg
        vx = b[0] - a[0]
        vy = b[1] - a[1]

        out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        if t0 > eps:
            p = (a[0], a[1])
            q = (a[0] + vx * t0, a[1] + vy * t0)
            if math.hypot(q[0] - p[0], q[1] - p[1]) > eps:
                out.append((p, q))
        if t1 < 1.0 - eps:
            p = (a[0] + vx * t1, a[1] + vy * t1)
            q = (b[0], b[1])
            if math.hypot(q[0] - p[0], q[1] - p[1]) > eps:
                out.append((p, q))
        return out

    def _overlap_interval_on_base(
        self,
        base_seg: Tuple[Tuple[float, float], Tuple[float, float]],
        cover_seg: Tuple[Tuple[float, float], Tuple[float, float]],
    ) -> Optional[Tuple[float, float]]:
        """Return overlap interval on base segment parameter t in [0,1], if colinear."""
        a, b = base_seg
        c, d = cover_seg
        abx = b[0] - a[0]
        aby = b[1] - a[1]
        len2 = abx * abx + aby * aby
        if len2 < 1e-12:
            return None

        tol = 1e-3
        cross_c = abs((c[0] - a[0]) * aby - (c[1] - a[1]) * abx)
        cross_d = abs((d[0] - a[0]) * aby - (d[1] - a[1]) * abx)
        if cross_c > tol or cross_d > tol:
            return None

        tc = ((c[0] - a[0]) * abx + (c[1] - a[1]) * aby) / len2
        td = ((d[0] - a[0]) * abx + (d[1] - a[1]) * aby) / len2
        lo = max(0.0, min(tc, td))
        hi = min(1.0, max(tc, td))
        if hi - lo <= 1e-5:
            return None
        return (lo, hi)

    def _segment_key(
        self, seg: Tuple[Tuple[float, float], Tuple[float, float]]
    ) -> str:
        (x1, y1), (x2, y2) = seg
        p = (round(x1, 3), round(y1, 3))
        q = (round(x2, 3), round(y2, 3))
        seq = f"{p[0]},{p[1]}->{q[0]},{q[1]}"
        rev = f"{q[0]},{q[1]}->{p[0]},{p[1]}"
        return min(seq, rev)

    def _segments_to_path_d(
        self, segs: List[Tuple[Tuple[float, float], Tuple[float, float]]]
    ) -> str:
        cmds = []
        for (x1, y1), (x2, y2) in segs:
            cmds.append(f"M {x1:.6f} {y1:.6f} L {x2:.6f} {y2:.6f}")
        return " ".join(cmds)

    def _extract_svg_path_points(self, d: str) -> List[Tuple[float, float]]:
        """Extract representative points from SVG path data (abs+relative commands)."""
        tokens = re.findall(
            r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",
            d,
        )
        if not tokens:
            return []

        points: List[Tuple[float, float]] = []
        i = 0
        cmd = ""
        cx = 0.0
        cy = 0.0
        sx = 0.0
        sy = 0.0

        def is_cmd(tok: str) -> bool:
            return len(tok) == 1 and tok.isalpha()

        def add_point(x: float, y: float) -> None:
            points.append((x, y))

        def read_float() -> float:
            nonlocal i
            val = float(tokens[i])
            i += 1
            return val

        while i < len(tokens):
            tok = tokens[i]
            if is_cmd(tok):
                cmd = tok
                i += 1
            elif not cmd:
                i += 1
                continue

            if cmd in ("M", "m"):
                first = True
                while i + 1 < len(tokens) and not is_cmd(tokens[i]):
                    x = read_float()
                    y = read_float()
                    if cmd == "m":
                        x += cx
                        y += cy
                    cx, cy = x, y
                    add_point(cx, cy)
                    if first:
                        sx, sy = cx, cy
                        first = False
                    else:
                        # Subsequent pairs are treated as line-to.
                        pass
                cmd = "L" if cmd == "M" else "l"

            elif cmd in ("L", "l"):
                while i + 1 < len(tokens) and not is_cmd(tokens[i]):
                    x = read_float()
                    y = read_float()
                    if cmd == "l":
                        x += cx
                        y += cy
                    cx, cy = x, y
                    add_point(cx, cy)

            elif cmd in ("H", "h"):
                while i < len(tokens) and not is_cmd(tokens[i]):
                    x = read_float()
                    if cmd == "h":
                        x += cx
                    cx = x
                    add_point(cx, cy)

            elif cmd in ("V", "v"):
                while i < len(tokens) and not is_cmd(tokens[i]):
                    y = read_float()
                    if cmd == "v":
                        y += cy
                    cy = y
                    add_point(cx, cy)

            elif cmd in ("C", "c"):
                while i + 5 < len(tokens) and not is_cmd(tokens[i]):
                    x1 = read_float()
                    y1 = read_float()
                    x2 = read_float()
                    y2 = read_float()
                    x = read_float()
                    y = read_float()
                    if cmd == "c":
                        x1 += cx
                        y1 += cy
                        x2 += cx
                        y2 += cy
                        x += cx
                        y += cy
                    add_point(x1, y1)
                    add_point(x2, y2)
                    cx, cy = x, y
                    add_point(cx, cy)

            elif cmd in ("S", "s", "Q", "q"):
                # S: x2 y2 x y ; Q: x1 y1 x y
                while i + 3 < len(tokens) and not is_cmd(tokens[i]):
                    x1 = read_float()
                    y1 = read_float()
                    x = read_float()
                    y = read_float()
                    if cmd.islower():
                        x1 += cx
                        y1 += cy
                        x += cx
                        y += cy
                    add_point(x1, y1)
                    cx, cy = x, y
                    add_point(cx, cy)

            elif cmd in ("T", "t"):
                while i + 1 < len(tokens) and not is_cmd(tokens[i]):
                    x = read_float()
                    y = read_float()
                    if cmd == "t":
                        x += cx
                        y += cy
                    cx, cy = x, y
                    add_point(cx, cy)

            elif cmd in ("A", "a"):
                # A: rx ry rot large-arc sweep x y
                while i + 6 < len(tokens) and not is_cmd(tokens[i]):
                    _rx = read_float()
                    _ry = read_float()
                    _rot = read_float()
                    _laf = read_float()
                    _sf = read_float()
                    x = read_float()
                    y = read_float()
                    if cmd == "a":
                        x += cx
                        y += cy
                    cx, cy = x, y
                    add_point(cx, cy)

            elif cmd in ("Z", "z"):
                cx, cy = sx, sy
                add_point(cx, cy)

            else:
                # Unknown token sequence, move forward defensively.
                i += 1

        return points

    def _render_isometric(self, svg: SVGWriter) -> None:
        """Render isometric view with proper silhouette."""
        renderer = IsometricRenderer(
            self.layout.VIEW_WIDTH,
            self.layout.VIEW_HEIGHT,
            self.scale
        )

        # For isometric, use a diagonal view direction
        # Standard isometric looks from direction (1, 1, 1) normalized
        import math
        norm = math.sqrt(1**2 + 1**2 + 1**2)
        iso_view_dir = (1/norm, 1/norm, 1/norm)

        # Set current view direction for silhouette computation
        self._current_view_dir = iso_view_dir

        # Compute silhouette for isometric view
        try:
            visible_edges, hidden_edges = self._compute_silhouette_occ(
                self.model.val() if isinstance(self.model, cadquery.Workplane) else self.model,
                iso_view_dir,
                self.model_center
            )
        except:
            # Fallback to all edges
            visible_edges = self._get_model_edges()
            hidden_edges = []

        # Project edges
        projected_visible = []
        for edge in visible_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = True
                # Add depth for occlusion detection (use midpoint)
                # For isometric view, we use a combination of all 3 axes
                # For (1,1,1) view direction, larger Y means closer to viewer
                projected_edge.depth = (edge.x1 + edge.x2 + edge.y1 + edge.y2 + edge.z1 + edge.z2) / 6
                projected_visible.append(projected_edge)

        projected_hidden = []
        for edge in hidden_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = False
                # Add depth for occlusion detection
                projected_edge.depth = (edge.x1 + edge.x2 + edge.y1 + edge.y2 + edge.z1 + edge.z2) / 6
                projected_hidden.append(projected_edge)

        # Deduplicate edges
        projected_visible = self._deduplicate_edges(projected_visible)
        projected_hidden = self._deduplicate_edges(projected_hidden)

        # Center in view
        offset_x = self.layout.VIEW_WIDTH / 2
        offset_y = self.layout.VIEW_HEIGHT / 2

        # Draw visible edges first
        for edge in projected_visible:
            svg.add_line(
                edge.x1 + offset_x, edge.y1 + offset_y,
                edge.x2 + offset_x, edge.y2 + offset_y,
                stroke_width=0.35,
            )

        # Draw hidden edges with dashed lines (width 0.25 for hidden, 0.35 for visible)
        for edge in projected_hidden:
            svg.add_line(
                edge.x1 + offset_x, edge.y1 + offset_y,
                edge.x2 + offset_x, edge.y2 + offset_y,
                stroke_width=0.25,
                dash_array="5,2"
            )

    def _render_top(self, svg: SVGWriter) -> None:
        """Render top (plan) view with proper silhouette."""
        renderer = OrthographicRenderer(
            self.layout.VIEW_WIDTH,
            self.layout.VIEW_HEIGHT,
            self.scale,
            ViewDirection.TOP
        )

        # Compute silhouette edges
        visible_edges, hidden_edges = self._compute_silhouette_for_view("top")

        # Project all edges
        projected_visible = []
        projected_hidden = []

        for edge in visible_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = True
                # Add depth - for TOP view, Z is depth axis (height)
                projected_edge.depth = (edge.z1 + edge.z2) / 2
                projected_visible.append(projected_edge)

        for edge in hidden_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = False
                # Preserve the hidden_from_silhouette attribute
                projected_edge.hidden_from_silhouette = getattr(edge, 'hidden_from_silhouette', False)
                projected_edge.depth = (edge.z1 + edge.z2) / 2
                projected_hidden.append(projected_edge)

        # Combine and do hidden line detection
        all_projected = projected_visible + projected_hidden

        # Additional hidden line detection
        detector = HiddenLineDetector(renderer)
        all_projected = detector.process_silhouette(projected_visible, projected_hidden)

        # Deduplicate edges
        all_projected = self._deduplicate_edges(all_projected)

        # Center in view
        offset_x = self.layout.VIEW_WIDTH / 2
        offset_y = self.layout.VIEW_HEIGHT / 2

        # Separate circle edges from line edges for proper rendering
        circle_edges = []
        line_edges = []

        for edge in all_projected:
            # Check if this edge is part of a circle (has center_x, center_y, radius)
            if (hasattr(edge, 'is_circle') and edge.is_circle and
                hasattr(edge, 'radius') and edge.radius > 0):
                # Avoid duplicate circles - only keep first edge of each circle
                # Use center coordinates as key
                circle_key = (round(edge.center_x, 3), round(edge.center_y, 3),
                             round(edge.radius, 3))
                if not any((round(e.center_x, 3), round(e.center_y, 3),
                           round(e.radius, 3)) == circle_key for e in circle_edges):
                    circle_edges.append(edge)
            else:
                line_edges.append(edge)

        # Render circle edges as proper circles
        for edge in circle_edges:
            # Project circle center to 2D
            projected_center = renderer.project_point(
                edge.center_x, edge.center_y, edge.center_z
            )
            if projected_center:
                cx = projected_center.x + offset_x
                cy = projected_center.y + offset_y
                r = edge.radius * self.scale  # Apply scale to radius

                # Determine line width: visible = 0.35, hidden = 0.25
                stroke_width = 0.25 if not edge.is_visible else 0.35
                dash = "5,2" if not edge.is_visible else None

                svg.add_circle(
                    cx, cy, r,
                    stroke_width=stroke_width,
                    dash_array=dash
                )

        # Render line edges as before
        for edge in line_edges:
            dash = "5,2" if not edge.is_visible else None
            # Determine line width: visible = 0.35, hidden = 0.25
            stroke_width = 0.25 if not edge.is_visible else 0.35
            svg.add_line(
                edge.x1 + offset_x, edge.y1 + offset_y,
                edge.x2 + offset_x, edge.y2 + offset_y,
                stroke_width=stroke_width,
                dash_array=dash
            )

    def _render_front(self, svg: SVGWriter) -> None:
        """Render front view with proper silhouette."""
        renderer = OrthographicRenderer(
            self.layout.VIEW_WIDTH,
            self.layout.VIEW_HEIGHT,
            self.scale,
            ViewDirection.FRONT
        )

        # Compute silhouette edges
        visible_edges, hidden_edges = self._compute_silhouette_for_view("front")

        # Project all edges
        projected_visible = []
        projected_hidden = []

        for edge in visible_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = True
                # Add depth - for FRONT view, Y is depth axis (depth)
                projected_edge.depth = (edge.y1 + edge.y2) / 2
                projected_visible.append(projected_edge)

        for edge in hidden_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = False
                projected_edge.depth = (edge.y1 + edge.y2) / 2
                projected_hidden.append(projected_edge)

        # Combine and do hidden line detection
        all_projected = projected_visible + projected_hidden

        # Additional hidden line detection
        detector = HiddenLineDetector(renderer)
        all_projected = detector.process_silhouette(projected_visible, projected_hidden)

        # Deduplicate edges
        all_projected = self._deduplicate_edges(all_projected)

        # Center in view
        offset_x = self.layout.VIEW_WIDTH / 2
        offset_y = self.layout.VIEW_HEIGHT / 2

        for edge in all_projected:
            dash = "5,2" if not edge.is_visible else None
            svg.add_line(
                edge.x1 + offset_x, edge.y1 + offset_y,
                edge.x2 + offset_x, edge.y2 + offset_y,
                stroke_width=0.35,
                dash_array=dash
            )

    def _render_right(self, svg: SVGWriter) -> None:
        """Render right side view with proper silhouette."""
        renderer = OrthographicRenderer(
            self.layout.VIEW_WIDTH,
            self.layout.VIEW_HEIGHT,
            self.scale,
            ViewDirection.RIGHT
        )

        # Compute silhouette edges
        visible_edges, hidden_edges = self._compute_silhouette_for_view("right")

        # Project all edges
        projected_visible = []
        projected_hidden = []

        for edge in visible_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = True
                # Add depth - for RIGHT view, X is depth axis
                projected_edge.depth = (edge.x1 + edge.x2) / 2
                projected_visible.append(projected_edge)

        for edge in hidden_edges:
            projected_edge = renderer.project_edge(
                (edge.x1, edge.y1, edge.z1),
                (edge.x2, edge.y2, edge.z2)
            )
            if projected_edge:
                projected_edge.is_visible = False
                projected_edge.depth = (edge.x1 + edge.x2) / 2
                projected_hidden.append(projected_edge)

        # Combine and do hidden line detection
        all_projected = projected_visible + projected_hidden

        # Additional hidden line detection
        detector = HiddenLineDetector(renderer)
        all_projected = detector.process_silhouette(projected_visible, projected_hidden)

        # Deduplicate edges
        all_projected = self._deduplicate_edges(all_projected)

        # Center in view
        offset_x = self.layout.VIEW_WIDTH / 2
        offset_y = self.layout.VIEW_HEIGHT / 2

        for edge in all_projected:
            dash = "5,2" if not edge.is_visible else None
            svg.add_line(
                edge.x1 + offset_x, edge.y1 + offset_y,
                edge.x2 + offset_x, edge.y2 + offset_y,
                stroke_width=0.35,
                dash_array=dash
            )

    def _deduplicate_edges(self, edges: List[Edge2D], tolerance: float = 1e-6) -> List[Edge2D]:
        """Remove duplicate edges from the list.

        Args:
            edges: List of Edge2D objects
            tolerance: Tolerance for comparing coordinates

        Returns:
            List of unique edges
        """
        unique = []
        seen = set()

        for edge in edges:
            # Normalize edge: sort endpoints to catch reversed duplicates
            p1 = (round(edge.x1, 6), round(edge.y1, 6))
            p2 = (round(edge.x2, 6), round(edge.y2, 6))

            # Create a normalized key (sorted endpoints)
            key = tuple(sorted([p1, p2]))

            if key not in seen:
                seen.add(key)
                unique.append(edge)

        return unique

    def _get_model_edges(self, edge_type: str = "all") -> List:
        """Get edges from the model with optional silhouette filtering.

        Args:
            edge_type: Type of edges to return - "all", "silhouette", "outline"

        Returns:
            List of edge objects
        """
        if self.model is None:
            return []

        # Get the underlying OCC shape
        if isinstance(self.model, cadquery.Workplane):
            shape = self.model.val()
        elif isinstance(self.model, cadquery.Solid):
            shape = self.model
        else:
            return []

        # Get all edges
        edges = shape.Edges()

        # If we want silhouette edges, we need to use OCC API
        if edge_type in ("silhouette", "outline"):
            return self._get_silhouette_edges(shape, edges, edge_type)

        # Offset edges to center
        cx, cy, cz = self.model_center

        # Create edge objects with center offset
        result = []
        for edge in edges:
            verts = edge.Vertices()  # Call as method
            if len(verts) >= 2:
                v1, v2 = verts[0], verts[-1]
                result.append(type('Edge', (), {
                    'x1': v1.X - cx,
                    'y1': v1.Y - cy,
                    'z1': v1.Z - cz,
                    'x2': v2.X - cx,
                    'y2': v2.Y - cy,
                    'z2': v2.Z - cz,
                })())

        return result

    def _get_silhouette_edges(self, shape, all_edges, edge_type: str = "silhouette") -> List:
        """Get silhouette/outline edges from the shape using OCC.

        Args:
            shape: The OCC shape
            all_edges: All edges from the shape
            edge_type: "silhouette" or "outline"

        Returns:
            List of silhouette/outline edge objects
        """
        try:
            from OCC.Core import BRepIntCurveSurface
            from OCC.Core import BRepBuilderAPI, BRep_Tool
            from OCC.Core.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
            from OCC.Core.TopExp import TopExp_Explorer
            from OCC.Core.TopAbs import TopAbs_EDGE
            from OCC.Core.Geom import Geom_SphericalSurface, Geom_Curve
            from OCC.Core.gp import gp_Dir, gp_Pnt
            import numpy as np
        except ImportError:
            # Fallback to all edges if OCC not directly accessible
            return self._get_model_edges("all")

        cx, cy, cz = self.model_center

        # For orthographic views, we compute silhouette based on face normals
        # Silhouette edge: where one adjacent face is visible and one is hidden

        # First, get all faces
        faces = shape.Faces()

        # Build a map of edges to their adjacent faces
        edge_face_map = {}  # edge_index -> [(face, orientation), ...]

        for face_idx, face in enumerate(faces):
            face_edges = face.Edges()
            for edge_idx, edge in enumerate(face_edges):
                if edge_idx not in edge_face_map:
                    edge_face_map[edge_idx] = []
                edge_face_map[edge_idx].append(face)

        # Calculate face normals and determine visibility for each view direction
        result = []

        # For each view direction (TOP, FRONT, RIGHT), we compute silhouette
        # Silhouette: edge where adjacent faces have different visibility

        # Get all unique vertices from all edges
        verts_map = {}  # edge_idx -> (v1, v2)
        for edge_idx, edge in enumerate(all_edges):
            verts = edge.Vertices()
            if len(verts) >= 2:
                verts_map[edge_idx] = (verts[0], verts[-1])

        # We'll compute silhouette for the current view direction
        # by checking face normals against the view direction
        # For now, let's implement a simpler but effective approach

        # Use projected edges and compute which are on the boundary
        return self._get_outline_edges_by_projection(shape, all_edges)

    def _get_outline_edges_by_projection(self, shape, all_edges) -> List:
        """Get outline edges by projecting and finding boundary.

        This method projects all edges and finds which ones form the
        outer boundary of the projection - the silhouette.

        Args:
            shape: The OCC shape
            all_edges: All edges

        Returns:
            List of outline edge objects
        """
        import numpy as np

        cx, cy, cz = self.model_center

        # Project all edges to find the outline
        # An outline edge is one that is on the "outside" of the projection

        # Group edges by their 2D projection
        projected_groups = {}

        for edge in all_edges:
            verts = edge.Vertices()
            if len(verts) < 2:
                continue

            v1, v2 = verts[0], verts[-1]
            x1, y1, z1 = v1.X - cx, v1.Y - cy, v1.Z - cz
            x2, y2, z2 = v2.X - cx, v2.Y - cy, v2.Z - cz

            # For now, we'll use a simple approach: keep edges that could be outline
            # by checking if they're at the max/min extent in any direction

            # Actually, let's do this more systematically:
            # We'll compute the 2D bounding box of each edge's projection
            # and keep only those that form the outer boundary

        # For a proper implementation, we need to:
        # 1. Project all edges
        # 2. Find overlapping edges
        # 3. Keep only the outermost ones

        # Let's use a simpler and more robust approach:
        # Get the mesh and compute silhouette from mesh edges

        # Use triangulation to get surface edges
        try:
            # Try to get triangulated representation for silhouette
            return self._get_edges_from_triangulation(shape)
        except:
            # Fallback: return all edges
            return self._get_model_edges("all")

    def _get_edges_from_triangulation(self, shape) -> List:
        """Get silhouette edges from triangulated representation.

        Args:
            shape: The OCC shape

        Returns:
            List of edge objects
        """
        try:
            from OCC.Core import BRepMesh
            from OCC.Core import TopAbs
            from OCC.Core.Poly import Poly_Array1OfTriangle
            from OCC.Core.TopExp import TopExp_Explorer
            from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE
        except ImportError:
            return self._get_model_edges("all")

        cx, cy, cz = self.model_center

        # Mesh the shape
        mesh = BRepMesh.BRepMesh_Incremental(shape, 0.1)

        # Get mesh triangles
        # The edges of the mesh triangles that appear only once are silhouette edges

        result = []
        return result

    def _get_model_faces(self):
        """Get faces from the model for silhouette computation.

        Returns:
            List of faces
        """
        if self.model is None:
            return []

        if isinstance(self.model, cadquery.Workplane):
            shape = self.model.val()
        elif isinstance(self.model, cadquery.Solid):
            shape = self.model
        else:
            return []

        return shape.Faces()

    def _compute_silhouette_for_view(self, view_dir: str) -> List:
        """Compute silhouette edges for a specific view direction.

        This computes:
        1. Outline/Silhouette edges - visible boundary edges
        2. Seam edges - internal edges on curved surfaces
        3. Hidden edges - occluded by other geometry

        Args:
            view_dir: View direction - "top", "front", "right"

        Returns:
            Tuple of (visible_edges, hidden_edges) where each is a list of edge objects
        """
        if self.model is None:
            return [], []

        # Get the OCC shape
        if isinstance(self.model, cadquery.Workplane):
            shape = self.model.val()
        elif isinstance(self.model, cadquery.Solid):
            shape = self.model
        else:
            return [], []

        cx, cy, cz = self.model_center

        # Get view direction vector (direction from model TOWARD viewer)
        # This is the direction pointing from model toward the viewer
        # For standard orthographic views:
        # - top: viewer at +Y, direction is (0, 1, 0) - looking down from above
        # - front: viewer at +Z, direction is (0, 0, 1) - looking from front
        # - right: viewer at +X, direction is (1, 0, 0) - looking from right side
        view_vectors = {
            "top": (0, 1, 0),     # From +Y looking toward model
            "front": (0, 0, 1),   # From +Z looking toward model
            "right": (1, 0, 0),   # From +X looking toward model
        }
        view_dir_vec = view_vectors.get(view_dir, (0, 1, 0))

        # Set current view direction for silhouette computation
        self._current_view_dir = view_dir_vec

        try:
            # Try to use OCC silhouette computation
            visible_edges, hidden_edges = self._compute_silhouette_occ(shape, view_dir_vec, (cx, cy, cz))

            # Add cylinder silhouette lines (for cylindrical faces)
            cylinder_silhouette = self._generate_cylinder_silhouette(shape, view_dir_vec, (cx, cy, cz))
            visible_edges.extend(cylinder_silhouette)

            return visible_edges, hidden_edges
        except Exception as e:
            # Fallback to all edges
            all_edges = self._get_model_edges("all")
            return all_edges, []

    def _compute_silhouette_occ(self, shape, view_dir, center) -> Tuple[List, List]:
        """Compute silhouette using CadQuery API.

        Args:
            shape: OCC shape
            view_dir: View direction tuple (x, y, z)
            center: Model center (cx, cy, cz)

        Returns:
            Tuple of (visible_edges, hidden_edges)
        """
        cx, cy, cz = center
        vx, vy, vz = view_dir

        # Get all faces
        faces = shape.Faces()

        # For each face, compute if it's visible from the view direction
        # Visibility is determined by the face's normal pointing toward the viewer
        # (i.e., the face is "facing" the viewer)
        face_visible = {}  # face_index -> bool
        face_normals = {}  # face_index -> normal vector
        face_centers = {}  # face_index -> center point

        # Determine which axis is the depth axis based on view direction
        # and compute face "frontness" based on position
        # After fixing the view directions:
        # - TOP view: viewer at +Y, depth axis is Z (height)
        # - FRONT view: viewer at +Z, depth axis is Y (depth)
        # - RIGHT view: viewer at +X, depth axis is X (width)
        depth_axis = None
        # Determine which axis is the depth axis based on view direction
        # After fixing the view directions:
        # - TOP view: viewer at +Y, depth axis is Z (height)
        # - FRONT view: viewer at +Z, depth axis is Y (depth)
        # - RIGHT view: viewer at +X, depth axis is X (width)
        # - ISOMETRIC view: diagonal direction (1,1,1), use combined depth
        depth_axis = None
        if abs(vy) > 0.5:  # Viewing from Y direction (top/bottom view)
            depth_axis = 'z'  # For top view, depth is Z (height)
        elif abs(vz) > 0.5:  # Viewing from Z direction (front/back view)
            depth_axis = 'y'  # For front view, depth is Y (depth)
        elif abs(vx) > 0.5:  # Viewing from X direction (left/right view)
            depth_axis = 'x'  # For right view, depth is X (width)
        else:
            # Isometric or other diagonal view - use combined depth
            depth_axis = 'xyz'

        for face_idx, face in enumerate(faces):
            try:
                # Get face normal using CadQuery API
                uv_bounds = face.uvBounds()
                normal_tuple = face.normalAt(uv_bounds[0], uv_bounds[2])
                normal = normal_tuple[0]  # Get direction vector

                face_normals[face_idx] = (normal.x, normal.y, normal.z)

                # Get face center for depth-based visibility
                try:
                    center_point = face.Center()
                    face_centers[face_idx] = (center_point.x, center_point.y, center_point.z)
                except:
                    face_centers[face_idx] = (0, 0, 0)

                # Dot product with view direction to check if face faces the viewer
                dot = normal.x * vx + normal.y * vy + normal.z * vz

                # A face is visible if its normal points toward the viewer
                # OR if it's on the "front" side in the depth direction
                face_visible[face_idx] = dot > 0

                # Override with position-based check: faces on the "front" side are visible
                face_center = face_centers[face_idx]
                if depth_axis == 'z':
                    # For top view: faces with larger Z (higher) are closer to viewer
                    face_visible[face_idx] = face_visible[face_idx] or (face_center[2] > cz)
                elif depth_axis == 'y':
                    # For front view: faces with larger Y are closer to viewer
                    face_visible[face_idx] = face_visible[face_idx] or (face_center[1] > cy)
                elif depth_axis == 'x':
                    # For right view: faces with larger X are closer to viewer
                    face_visible[face_idx] = face_visible[face_idx] or (face_center[0] > cx)
                elif depth_axis == 'xyz':
                    # For isometric view: use dot product with view direction to determine visibility
                    # A face is visible if it faces the viewer (positive dot product)
                    # and if it's in the "front" half of the model
                    face_visible[face_idx] = dot > 0

            except Exception as e:
                # Default to visible if we can't determine
                face_visible[face_idx] = True
                face_normals[face_idx] = (0, 0, 1)
                face_centers[face_idx] = (0, 0, 0)

        # Build edge-to-faces map
        edge_map = {}  # edge_hash -> {'edge': edge, 'faces': [face_idx], 'is_hole': bool}

        for face_idx, face in enumerate(faces):
            # First: Get inner wire edges (holes) - BEFORE outer edges
            # This ensures holes are properly marked
            try:
                # Try innerWires method first
                try:
                    inner_wires = face.innerWires()
                    for wire in inner_wires:
                        inner_edges = wire.Edges()
                        for edge in inner_edges:
                            edge_hash = hash(edge)
                            if edge_hash not in edge_map:
                                edge_map[edge_hash] = {'edge': edge, 'faces': [], 'is_hole': True}
                            edge_map[edge_hash]['faces'].append(face_idx)
                except:
                    # Fallback: try getting all wires
                    wires = face.Wires()
                    if len(wires) > 1:
                        # Multiple wires means there are holes
                        for wire_idx in range(1, len(wires)):
                            inner_edges = wires[wire_idx].Edges()
                            for edge in inner_edges:
                                edge_hash = hash(edge)
                                if edge_hash not in edge_map:
                                    edge_map[edge_hash] = {'edge': edge, 'faces': [], 'is_hole': True}
                                edge_map[edge_hash]['faces'].append(face_idx)
            except:
                pass

            # Second: Get outer wire edges
            face_edges = face.Edges()
            for edge in face_edges:
                edge_hash = hash(edge)
                if edge_hash not in edge_map:
                    edge_map[edge_hash] = {'edge': edge, 'faces': [], 'is_hole': False}
                edge_map[edge_hash]['faces'].append(face_idx)

        # Get all vertices from edges
        edges = shape.Edges()

        # Now classify each edge
        outline_edges = []  # Visible silhouette edges
        seam_edges = []     # Internal visible edges
        hidden_edges = []   # Hidden edges

        for edge_hash, edge_data in edge_map.items():
            edge = edge_data['edge']
            adj_face_indices = edge_data['faces']
            is_hole = edge_data.get('is_hole', False)

            verts = edge.Vertices()

            # Handle closed edges (circles) - they have only 1 vertex
            if len(verts) == 1:
                # For a closed edge (circle), we need to determine visibility
                # based on the face(s) it's attached to
                circle_edges = self._create_edge_obj_from_single_vertex(edge, cx, cy, cz)

                # For circles with >=2 adjacent faces, check visibility based on Z coordinate
                if len(adj_face_indices) >= 2:
                    model_bbox = getattr(self, '_model_bbox', None)
                    if model_bbox:
                        z_min, z_max = model_bbox[2], model_bbox[5]
                        cx_center, cy_center, cz_center = self.model_center

                        center_z = getattr(circle_edges[0], 'center_z', 0) if circle_edges else 0
                        # Add back the offset to get actual Z
                        actual_z = center_z + cz_center

                        # For top view (viewing from +Y): higher Z = closer to viewer
                        # Circle at z_max (surface) is visible
                        # Circle inside (not at z_max) is hidden (blind hole bottom or internal)
                        if actual_z >= z_max - 0.1:
                            outline_edges.extend(circle_edges)
                        else:
                            # Mark as hidden from silhouette so HiddenLineDetector preserves this
                            for e in circle_edges:
                                e.hidden_from_silhouette = True
                            hidden_edges.extend(circle_edges)
                    else:
                        outline_edges.extend(circle_edges)
                elif is_hole:
                    # Hole with only 1 adjacent face: blind hole edge
                    # Check if the face is visible
                    face_idx = adj_face_indices[0] if adj_face_indices else None
                    if face_idx and face_visible.get(face_idx, True):
                        outline_edges.extend(circle_edges)
                    else:
                        hidden_edges.extend(circle_edges)
                elif len(adj_face_indices) == 1:
                    # Single face - check if that face is visible
                    face_idx = adj_face_indices[0]
                    if face_visible.get(face_idx, True):
                        outline_edges.extend(circle_edges)
                    else:
                        hidden_edges.extend(circle_edges)
                elif len(adj_face_indices) >= 2:
                    # Circle with 2 adjacent faces (e.g., cylinder top/bottom or hole)
                    # For a hole in top view:
                    # - Top edge of hole (higher Z) is visible as outline
                    # - Bottom edge of hole (lower Z) is hidden (dashed)
                    # Get the center of the circle
                    if circle_edges:
                        center_z = getattr(circle_edges[0], 'center_z', 0)
                        center_y = getattr(circle_edges[0], 'center_y', 0)
                        center_x = getattr(circle_edges[0], 'center_x', 0)
                    else:
                        center_z = center_y = center_x = 0

                    # Get view direction
                    vx, vy, vz = getattr(self, '_current_view_dir', (0, 1, 0))

                    # Get model bounding box to find the extent
                    model_bbox = getattr(self, '_model_bbox', None)

                    # Determine visibility based on view direction and circle position
                    is_visible = False

                    if abs(vy) > 0.5:  # Top view (viewing from +Y)
                        # For top view: higher Z = closer to viewer
                        # Get the max Z from bbox
                        if model_bbox:
                            z_min, z_max = model_bbox[2], model_bbox[5]
                            z_mid = (z_min + z_max) / 2
                            # For blind hole: bottom edge is at z=25, top edge at z=40
                            # The edge at z=40 is visible, edge at z=25 is hidden
                            # So we need to check if center_z is closer to max Z
                            # If center_z is close to z_max, visible; otherwise hidden
                            z_range = z_max - z_min
                            # If center_z is in the upper half, visible
                            is_visible = center_z > (z_range * 0.3)  # threshold
                    elif abs(vz) > 0.5:  # Front view (viewing from +Z)
                        # For front view: higher Y = closer to viewer
                        if model_bbox:
                            y_min, y_max = model_bbox[1], model_bbox[4]
                            y_range = y_max - y_min
                            is_visible = center_y > (y_range * 0.3)
                        else:
                            is_visible = center_y > 0
                    else:  # Right view (viewing from +X)
                        # For right view: higher X = closer to viewer
                        if model_bbox:
                            x_min, x_max = model_bbox[0], model_bbox[3]
                            x_range = x_max - x_min
                            is_visible = center_x > (x_range * 0.3)
                        else:
                            is_visible = center_y > 0

                    if is_visible:
                        outline_edges.extend(circle_edges)
                    else:
                        hidden_edges.extend(circle_edges)
                continue

            if len(verts) < 2:
                continue

            v1, v2 = verts[0], verts[-1]

            if len(adj_face_indices) < 2:
                # Boundary edge - on the outside of the model
                outline_edges.append(self._create_edge_obj(v1, v2, cx, cy, cz))
                continue

            # Get visibility of adjacent faces
            face1_visible = face_visible.get(adj_face_indices[0], True)
            face2_visible = face_visible.get(adj_face_indices[1], True)

            # Silhouette: one face visible, one hidden
            if face1_visible != face2_visible:
                outline_edges.append(self._create_edge_obj(v1, v2, cx, cy, cz))
            elif face1_visible and face2_visible:
                # Both visible - seam edge on curved surface
                seam_edges.append(self._create_edge_obj(v1, v2, cx, cy, cz))
            else:
                # Both hidden - potentially hidden edge
                hidden_edges.append(self._create_edge_obj(v1, v2, cx, cy, cz))

        return outline_edges + seam_edges, hidden_edges

    def _generate_cylinder_silhouette(self, shape, view_dir, center) -> List:
        """Generate silhouette edges for cylindrical faces.

        For cylindrical faces, the silhouette consists of two lines along the
        cylinder axis at the points where the surface normal is perpendicular
        to the view direction.

        Args:
            shape: OCC shape
            view_dir: View direction tuple (vx, vy, vz)
            center: Model center (cx, cy, cz)

        Returns:
            List of silhouette edge objects
        """
        cx, cy, cz = center
        vx, vy, vz = view_dir
        silhouette_edges = []

        try:
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            from OCP.GeomAbs import GeomAbs_Cylinder
        except ImportError:
            return []

        faces = shape.Faces()

        for face in faces:
            try:
                # Get the underlying OCC face
                occ_face = face.wrapped

                # Check if this is a cylindrical face
                surf = BRepAdaptor_Surface(occ_face, True)
                if surf.GetType() != GeomAbs_Cylinder:
                    continue

                # Get cylinder properties
                cylinder = surf.Cylinder()
                radius = cylinder.Radius()
                axis = cylinder.Axis()

                # Get axis direction and location
                cyl_axis = (axis.Direction().X(), axis.Direction().Y(), axis.Direction().Z())
                ax_pt = (axis.Location().X(), axis.Location().Y(), axis.Location().Z())

                # Get U,V bounds
                u_min, u_max, v_min, v_max = face.uvBounds()
                v_extent = v_max - v_min
                u_range = u_max - u_min

                # Check if this is a full cylinder (complete 2*pi angular range)
                if u_range < 5.5:  # Not a full cylinder (less than ~315 degrees)
                    continue

                # Calculate silhouette direction: view_dir x cyl_axis
                sil_dir_x = vy * cyl_axis[2] - vz * cyl_axis[1]
                sil_dir_y = vz * cyl_axis[0] - vx * cyl_axis[2]
                sil_dir_z = vx * cyl_axis[1] - vy * cyl_axis[0]

                # Normalize
                sil_len = (sil_dir_x**2 + sil_dir_y**2 + sil_dir_z**2) ** 0.5
                if sil_len < 1e-6:
                    # View direction is parallel to cylinder axis
                    continue

                sil_dir_x /= sil_len
                sil_dir_y /= sil_len
                sil_dir_z /= sil_len

                # Generate the two silhouette edges along the cylinder
                for sign in [1, -1]:
                    sx = ax_pt[0] + sign * radius * sil_dir_x
                    sy = ax_pt[1] + sign * radius * sil_dir_y
                    sz = ax_pt[2] + sign * radius * sil_dir_z

                    ex = ax_pt[0] + cyl_axis[0] * v_extent + sign * radius * sil_dir_x
                    ey = ax_pt[1] + cyl_axis[1] * v_extent + sign * radius * sil_dir_y
                    ez = ax_pt[2] + cyl_axis[2] * v_extent + sign * radius * sil_dir_z

                    edge_obj = type('Edge', (), {
                        'x1': sx - cx,
                        'y1': sy - cy,
                        'z1': sz - cz,
                        'x2': ex - cx,
                        'y2': ey - cy,
                        'z2': ez - cz,
                        'is_silhouette': True
                    })()

                    silhouette_edges.append(edge_obj)

            except Exception as e:
                continue

        return silhouette_edges

    def _create_edge_obj(self, v1, v2, cx, cy, cz):
        """Create an edge object with center offset."""
        return type('Edge', (), {
            'x1': v1.X - cx,
            'y1': v1.Y - cy,
            'z1': v1.Z - cz,
            'x2': v2.X - cx,
            'y2': v2.Y - cy,
            'z2': v2.Z - cz,
        })()

    def _create_edge_obj_from_single_vertex(self, edge, cx, cy, cz):
        """Create edge objects from a closed edge (circle) by tessellating it.

        Args:
            edge: Edge object (for closed edges)
            cx, cy, cz: Center offset

        Returns:
            List of edge objects approximating the circle
        """
        # For closed edges (circles), we need to tessellate them
        # Use positionAt to sample points along the circle
        num_samples = 16  # Number of segments to approximate circle (reduced from 36 for smaller file size)

        edges = []
        prev_pt = None

        # Calculate center point of the circle
        center_x, center_y, center_z = 0, 0, 0
        total_pts = 0
        first_pt = None

        try:
            for i in range(num_samples + 1):
                t = i / num_samples
                pt = edge.positionAt(t)
                if first_pt is None:
                    first_pt = pt
                center_x += pt.x
                center_y += pt.y
                center_z += pt.z
                total_pts += 1

            if total_pts > 0:
                center_x = center_x / total_pts - cx
                center_y = center_y / total_pts - cy
                center_z = center_z / total_pts - cz
        except:
            pass

        # Calculate radius
        radius = 0
        if first_pt is not None:
            radius = ((first_pt.x - cx - center_x) ** 2 +
                     (first_pt.y - cy - center_y) ** 2 +
                     (first_pt.z - cz - center_z) ** 2) ** 0.5

        try:
            for i in range(num_samples + 1):
                t = i / num_samples
                pt = edge.positionAt(t)

                if prev_pt is not None:
                    edges.append(type('Edge', (), {
                        'x1': prev_pt.x - cx,
                        'y1': prev_pt.y - cy,
                        'z1': prev_pt.z - cz,
                        'x2': pt.x - cx,
                        'y2': pt.y - cy,
                        'z2': pt.z - cz,
                        'center_x': center_x,
                        'center_y': center_y,
                        'center_z': center_z,
                        'radius': radius,
                        'is_circle': True,
                    })())
                prev_pt = pt
        except:
            pass

        return edges

    def _add_scale_annotation(self, svg: SVGWriter) -> None:
        """Add scale annotation to SVG."""
        calc = ScaleCalculator(self.layout.WIDTH, self.layout.HEIGHT)
        scale_str = calc.format_scale(self.scale)
        _, title_y, title_w, title_h = self._title_block_geometry()
        x = self.layout.MARGIN + title_w / 2.0
        y = title_y + title_h - 5.0

        svg.add_text(x, y, f"Scale: {scale_str}", font_size=3.5)

    def _add_title_block(self, svg: SVGWriter) -> None:
        """Add title block at bottom."""
        x, y, w, h = self._title_block_geometry()

        # Title block border
        svg.add_rect(x, y, w, h, stroke="#808080", stroke_width=0.7)

        # Title
        svg.add_text(x + 5, y + 8, self.name, font_size=4, text_anchor="start")
        svg.add_text(x + 5, y + 14, "Made by Orca", font_size=3, text_anchor="start")

        # Date
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        svg.add_text(x + w - 5, y + 8, date_str, font_size=3, text_anchor="end")

    def _title_block_geometry(self) -> Tuple[float, float, float, float]:
        """Get title block geometry aligned with the four viewport frames."""
        left = self.layout.MARGIN
        top = self.layout.VIEW_POSITIONS[2][1] + self.layout.VIEW_HEIGHT
        width = self.layout.AVAILABLE_WIDTH
        height = self.layout.TITLE_BLOCK_HEIGHT
        return (left, top, width, height)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Convert CadQuery/STEP models to technical SVG drawings and STEP files"
    )
    parser.add_argument("input", nargs="?", help="Input CadQuery Python file or STEP file")
    parser.add_argument("-o", "--output", help="Output SVG file", default="output.svg")
    parser.add_argument("-s", "--scale", type=float, help="Scale factor (overrides auto)")
    parser.add_argument("-n", "--name", help="Drawing title/name", default="CadQuery to SVG")
    parser.add_argument(
        "-p", "--paper-size", default="A3", choices=["A2", "A3", "A4"],
        help="Paper size (default: A3)"
    )
    parser.add_argument(
        "--code",
        default=None,
        help="CadQuery code string as input. If set, positional input is optional.",
    )
    parser.add_argument("-t", "--step", "--step-output",
                        dest="step_output",
                        help="Output STEP file path (.stp/.step). Defaults to same basename as SVG.")
    parser.add_argument("--svg-only", action="store_true",
                        help="Only generate SVG and skip STEP export.")

    args = parser.parse_args()

    # Create converter with custom name
    converter = CadQueryToSVG(name=args.name, paper_size=args.paper_size)

    # Load model
    try:
        if args.code is not None:
            converter.load_model_from_code(args.code)
        elif args.input:
            converter.load_model_from_file(args.input)
        else:
            raise ValueError("Either input file path or --code must be provided.")
    except Exception as e:
        print(f"Error loading model: {e}")
        sys.exit(1)

    # Override scale if specified
    if args.scale:
        converter.scale = args.scale

    # Default behavior: export both SVG and STEP.
    if args.svg_only:
        converter.render(args.output)
    else:
        converter.export_all(args.output, args.step_output)


if __name__ == "__main__":
    main()
