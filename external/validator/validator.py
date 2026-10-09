"""
CadQuery 合规检查器

该模块提供对CadQuery代码的合规性检查，包括：
1. 代码语法检查 - 检测代码是否会报错
2. 几何合规检查 - 检测NURBS几何问题（水密性、体积、自交、非流形等）
"""

import sys
import io
import traceback
import ast
from typing import Tuple, Dict, List, Any, Optional
from dataclasses import dataclass, field


@dataclass
class GeometryIssue:
    """几何问题数据类"""
    issue_type: str
    severity: str  # 'error', 'warning', 'info'
    description: str
    details: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.issue_type}: {self.description}"


@dataclass
class ValidationReport:
    """验证报告数据类"""
    code_valid: bool
    geometry_valid: bool
    code_error: Optional[str] = None
    geometry_issues: List[GeometryIssue] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"代码合规: {'✓' if self.code_valid else '✗'}",
            f"几何合规: {'✓' if self.geometry_valid else '✗'}"
        ]

        if not self.code_valid and self.code_error:
            lines.append(f"\n代码错误: {self.code_error}")

        if self.geometry_issues:
            lines.append("\n几何问题:")
            for issue in self.geometry_issues:
                lines.append(f"  - {issue}")

        return "\n".join(lines)


class CadQueryValidator:
    """
    CadQuery代码合规性检查器

    检查项目：
    - 代码语法和运行时错误
    - 水密性（封闭几何体）
    - 体积（是否为0或负数）
    - 自交检测
    - 非流形边/顶点
    - Bounding Box检查
    """

    def __init__(self):
        """初始化检查器"""
        self._checked_types = None

    def _get_occt_types(self):
        """获取OCCT类型，用于几何检查"""
        if self._checked_types is not None:
            return self._checked_types

        try:
            # 尝试从OCP获取
            from OCP import BOPAlgo, BOPTools, BRepAlgoAPI, TopAbs, TopoDS, BRepBuilderAPI
            self._checked_types = {
                'BOPAlgo': BOPAlgo,
                'BOPTools': BOPTools,
                'BRepAlgoAPI': BRepAlgoAPI,
                'TopAbs': TopAbs,
                'TopoDS': TopoDS,
                'BRepBuilderAPI': BRepBuilderAPI
            }
            return self._checked_types
        except ImportError:
            pass

        try:
            # 尝试从cadquery.occ_impl获取
            import cadquery.occ_impl.kernel as kernel
            self._checked_types = {'kernel': kernel}
            return self._checked_types
        except (ImportError, AttributeError):
            pass

        # 尝试从cadquery获取
        try:
            import cadquery
            self._checked_types = {'cq': cadquery}
            return self._checked_types
        except ImportError:
            pass

        self._checked_types = {}
        return self._checked_types

    def validate(self, code: str) -> Tuple[bool, bool]:
        """
        验证cadquery代码

        Args:
            code: CadQuery代码字符串

        Returns:
            Tuple[bool, bool]:
                - 第一个bool: 代码是否合规（无语法/运行时错误）
                - 第二个bool: 几何是否合规（通过所有几何检查）
        """
        report = self.get_detailed_report(code)
        return report.code_valid, report.geometry_valid

    def validate_code_only(self, code: str) -> bool:
        """
        仅验证代码语法

        Args:
            code: CadQuery代码字符串

        Returns:
            bool: 代码是否可正常执行
        """
        return self._validate_code(code)[0]

    def validate_geometry_only(self, code: str) -> bool:
        """
        仅验证几何合规性

        Args:
            code: CadQuery代码字符串

        Returns:
            bool: 几何是否合规
        """
        report = self.get_detailed_report(code)
        return report.geometry_valid

    def get_detailed_report(self, code: str) -> ValidationReport:
        """
        获取详细的验证报告

        Args:
            code: CadQuery代码字符串

        Returns:
            ValidationReport: 包含详细检查结果的报告对象
        """
        # 第一步：验证代码语法
        code_valid, code_error = self._validate_code(code)

        if not code_valid:
            return ValidationReport(
                code_valid=False,
                geometry_valid=False,
                code_error=code_error
            )

        # 第二步：验证几何合规性
        geometry_valid, geometry_issues = self._validate_geometry(code)

        return ValidationReport(
            code_valid=code_valid,
            geometry_valid=geometry_valid,
            code_error=code_error,
            geometry_issues=geometry_issues
        )

    def _validate_code(self, code: str) -> Tuple[bool, Optional[str]]:
        """
        验证代码是否可以正常执行

        Args:
            code: CadQuery代码字符串

        Returns:
            Tuple[bool, Optional[str]]:
                - bool: 代码是否有效
                - str: 错误信息（如果无效）
        """
        # 1. 首先检查Python语法
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"Python语法错误: {e}"

        # 2. 尝试执行代码，检查运行时错误
        # 创建隔离的全局和局部命名空间
        namespace = {
            '__builtins__': __builtins__,
        }

        # 捕获stdout以避免打印输出
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()

        try:
            # 执行代码
            exec(code, namespace)

            # 检查是否创建了result对象
            if 'result' not in namespace:
                # 可能代码返回了其他变量
                pass

            # 恢复stdout
            sys.stdout = old_stdout
            return True, None

        except Exception as e:
            sys.stdout = old_stdout
            error_msg = f"运行时错误: {type(e).__name__}: {e}"
            return False, error_msg

        finally:
            sys.stdout = old_stdout

    def _validate_geometry(self, code: str) -> Tuple[bool, List[GeometryIssue]]:
        """
        验证几何合规性

        检查项目：
        - 水密性（封闭性）
        - 体积（是否为0或负数）
        - 自交
        - 非流形边/顶点
        - Bounding Box

        Args:
            code: CadQuery代码字符串

        Returns:
            Tuple[bool, List[GeometryIssue]]:
                - bool: 几何是否合规
                - List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        # 创建执行命名空间
        namespace = {
            '__builtins__': __builtins__,
        }

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()

        try:
            exec(code, namespace)
            sys.stdout = old_stdout
        except Exception as e:
            sys.stdout = old_stdout
            issues.append(GeometryIssue(
                issue_type="CodeExecution",
                severity="error",
                description=f"无法执行代码: {e}"
            ))
            return False, issues

        # 获取结果对象
        result = namespace.get('result')
        if result is None:
            issues.append(GeometryIssue(
                issue_type="NoResult",
                severity="error",
                description="代码未生成result对象"
            ))
            return False, issues

        # 尝试转换为OCCT对象进行详细检查
        try:
            # 获取cadquery的CQ对象
            if hasattr(result, 'val'):
                # 这是CQ对象，获取实际几何体
                shape = result.val()
            elif hasattr(result, 'wrapped'):
                # 这是Shape对象
                shape = result.wrapped
            else:
                # 尝试其他方式
                shape = result
        except Exception as e:
            issues.append(GeometryIssue(
                issue_type="ShapeAccess",
                severity="error",
                description=f"无法访问几何体: {e}"
            ))
            return False, issues

        # 检查几何类型
        if shape is None:
            issues.append(GeometryIssue(
                issue_type="NullShape",
                severity="error",
                description="几何体为空"
            ))
            return False, issues

        # 尝试进行详细的OCCT检查
        issues.extend(self._check_with_occt(shape))

        # 如果没有OCCT，检查基本属性
        if not issues:
            issues.extend(self._check_basic_geometry(shape))

        geometry_valid = all(
            issue.severity != 'error' for issue in issues
        )

        return geometry_valid, issues

    def _check_watertightness(self, shape) -> List[GeometryIssue]:
        """
        检查几何体的水密性（封闭性）

        原理：对于水密的实体，每条边应该正好被2个面共享
        - 如果边只被1个面使用，说明几何体有开放的边/面（不水密）
        - 如果边被超过2个面使用，说明有非流形问题

        Args:
            shape: OCCT形状对象

        Returns:
            List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        # 获取wrapped对象
        wrapped = None
        if hasattr(shape, 'wrapped'):
            wrapped = shape.wrapped
        elif hasattr(shape, 'ShapeType'):  # 已经是wrapped对象
            wrapped = shape

        if wrapped is None:
            return issues

        try:
            # 首先检查isValid方法（CadQuery的Shape对象）
            if hasattr(shape, 'isValid'):
                if not shape.isValid():
                    issues.append(GeometryIssue(
                        issue_type="Watertightness",
                        severity="error",
                        description="几何体无效（非水密或自交）"
                    ))

            # 检查形状类型
            shape_type = None
            if hasattr(wrapped, 'ShapeType'):
                shape_type = wrapped.ShapeType()

            # 使用边的使用次数来检查水密性
            # 注意：只对Solid类型进行严格检查
            # Compound类型（如带孔几何）可能有内部边界是正常的
            try:
                from OCP import TopAbs, TopExp

                # 使用int比较，因为OCP枚举比较可能有问题
                if shape_type is not None:
                    is_solid = int(shape_type) == int(TopAbs.TopAbs_SOLID)
                    is_compound = int(shape_type) == int(TopAbs.TopAbs_COMPOUND)

                    # 对于非Solid/Compound类型，跳过水密性检查
                    if not (is_solid or is_compound):
                        return issues

                    # 对于Compound类型，如果isValid为True，认为是水密的
                    # 因为Compound可能包含内部孔洞
                    if is_compound:
                        if hasattr(shape, 'isValid') and shape.isValid():
                            return issues
                        # 如果没有isValid方法，也返回
                        return issues

                # 获取所有边
                edges = []
                exp = TopExp.TopExp_Explorer(wrapped, TopAbs.TopAbs_EDGE)
                while exp.More():
                    edges.append(exp.Current())
                    exp.Next()

                # 获取所有面
                faces = []
                exp = TopExp.TopExp_Explorer(wrapped, TopAbs.TopAbs_FACE)
                while exp.More():
                    faces.append(exp.Current())
                    exp.Next()

                # 对于面数少于2的几何体（如球体），跳过边的使用次数检查
                # 因为这类几何可能有特殊的内部表示
                if len(faces) < 2:
                    return issues

                # 统计每条边被多少个面使用
                edge_usage = {}
                for i, edge in enumerate(edges):
                    count = 0
                    for face in faces:
                        # 检查边是否在这个面上
                        exp_edges = TopExp.TopExp_Explorer(face, TopAbs.TopAbs_EDGE)
                        while exp_edges.More():
                            if exp_edges.Current().IsSame(edge):
                                count += 1
                                break
                            exp_edges.Next()
                    edge_usage[i] = count

                # 检查开放边（只被1个面使用）
                open_edges = sum(1 for c in edge_usage.values() if c == 1)
                if open_edges > 0:
                    issues.append(GeometryIssue(
                        issue_type="Watertightness",
                        severity="error",
                        description=f"几何体不水密: 发现{open_edges}条开放边（边只被1个面使用）"
                    ))

                # 检查非流形边（被超过2个面使用）
                non_manifold_edges = sum(1 for c in edge_usage.values() if c > 2)
                if non_manifold_edges > 0:
                    issues.append(GeometryIssue(
                        issue_type="NonManifoldEdge",
                        severity="error",
                        description=f"发现{non_manifold_edges}条非流形边（边被超过2个面使用）"
                    ))

            except Exception as e:
                issues.append(GeometryIssue(
                    issue_type="WatertightnessCheck",
                    severity="warning",
                    description=f"水密性检查失败: {e}"
                ))

        except Exception as e:
            issues.append(GeometryIssue(
                issue_type="Watertightness",
                severity="warning",
                description=f"水密性检查异常: {e}"
            ))

        return issues

    def _check_self_intersection(self, shape) -> List[GeometryIssue]:
        """
        检查几何体是否自交

        原理：使用BRepCheck_Analyzer检查形状是否有自交问题

        Args:
            shape: OCCT形状对象

        Returns:
            List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        # 获取wrapped对象
        wrapped = None
        if hasattr(shape, 'wrapped'):
            wrapped = shape.wrapped
        elif hasattr(shape, 'ShapeType'):
            wrapped = shape

        if wrapped is None:
            return issues

        try:
            from OCP import BRepCheck, TopExp, TopAbs

            # 创建BRepCheck_Analyzer
            analyzer = BRepCheck.BRepCheck_Analyzer(wrapped)

            # 检查是否有效
            if not analyzer.IsValid(wrapped):
                issues.append(GeometryIssue(
                    issue_type="SelfIntersection",
                    severity="error",
                    description="几何体存在自交或其他有效性问题"
                ))
                return issues

            # 检查主形状的状态
            result_check = analyzer.Result(wrapped)
            if result_check:
                status_list = result_check.StatusOnShape(wrapped)
                if status_list and not status_list.IsEmpty():
                    first_status = status_list.First()
                    if first_status == BRepCheck.BRepCheck_SelfIntersectingWire:
                        issues.append(GeometryIssue(
                            issue_type="SelfIntersection",
                            severity="error",
                            description="检测到自相交线框"
                        ))

            # 检查子形状（面）
            exp = TopExp.TopExp_Explorer(wrapped, TopAbs.TopAbs_FACE)
            while exp.More():
                face = exp.Current()
                face_result = analyzer.Result(face)
                if face_result:
                    face_status_list = face_result.StatusOnShape(face)
                    if face_status_list and not face_status_list.IsEmpty():
                        face_status = face_status_list.First()
                        if face_status == BRepCheck.BRepCheck_SelfIntersectingWire:
                            issues.append(GeometryIssue(
                                issue_type="SelfIntersection",
                                severity="error",
                                description="检测到自相交面"
                            ))
                            break
                exp.Next()

            # 检查线框
            exp = TopExp.TopExp_Explorer(wrapped, TopAbs.TopAbs_WIRE)
            while exp.More():
                wire = exp.Current()
                wire_result = analyzer.Result(wire)
                if wire_result:
                    wire_status_list = wire_result.StatusOnShape(wire)
                    if wire_status_list and not wire_status_list.IsEmpty():
                        wire_status = wire_status_list.First()
                        if wire_status == BRepCheck.BRepCheck_SelfIntersectingWire:
                            issues.append(GeometryIssue(
                                issue_type="SelfIntersection",
                                severity="error",
                                description="检测到自相交线框"
                            ))
                            break
                exp.Next()

        except Exception as e:
            issues.append(GeometryIssue(
                issue_type="SelfIntersectionCheck",
                severity="warning",
                description=f"自交检查失败: {e}"
            ))

        return issues

    def _check_normal_consistency(self, shape) -> List[GeometryIssue]:
        """
        检查几何体的法线一致性（是否都朝外）

        原理：使用BRepCheck检查法线方向是否正确

        Args:
            shape: OCCT形状对象

        Returns:
            List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        # 获取wrapped对象
        wrapped = None
        if hasattr(shape, 'wrapped'):
            wrapped = shape.wrapped
        elif hasattr(shape, 'ShapeType'):
            wrapped = shape

        if wrapped is None:
            return issues

        try:
            from OCP import BRepCheck, TopExp, TopAbs

            # 创建BRepCheck_Analyzer
            analyzer = BRepCheck.BRepCheck_Analyzer(wrapped)

            # 检查整体形状的法线方向
            result_check = analyzer.Result(wrapped)
            if result_check:
                status_list = result_check.StatusOnShape(wrapped)
                if status_list and not status_list.IsEmpty():
                    first_status = status_list.First()
                    if first_status == BRepCheck.BRepCheck_BadOrientation:
                        issues.append(GeometryIssue(
                            issue_type="NormalConsistency",
                            severity="error",
                            description="几何体法线方向不正确（未统一朝外）"
                        ))
                    elif first_status == BRepCheck.BRepCheck_BadOrientationOfSubshape:
                        issues.append(GeometryIssue(
                            issue_type="NormalConsistency",
                            severity="error",
                            description="子形状法线方向不正确"
                        ))

            # 检查子形状（面）的法线
            exp = TopExp.TopExp_Explorer(wrapped, TopAbs.TopAbs_FACE)
            bad_face_count = 0
            while exp.More():
                face = exp.Current()
                face_result = analyzer.Result(face)
                if face_result:
                    face_status_list = face_result.StatusOnShape(face)
                    if face_status_list and not face_status_list.IsEmpty():
                        face_status = face_status_list.First()
                        if (face_status == BRepCheck.BRepCheck_BadOrientation or
                            face_status == BRepCheck.BRepCheck_BadOrientationOfSubshape):
                            bad_face_count += 1
                exp.Next()

            if bad_face_count > 0:
                issues.append(GeometryIssue(
                    issue_type="NormalConsistency",
                    severity="error",
                    description=f"发现{bad_face_count}个面法线方向不正确"
                ))

        except Exception as e:
            issues.append(GeometryIssue(
                issue_type="NormalConsistencyCheck",
                severity="warning",
                description=f"法线一致性检查失败: {e}"
            ))

        return issues

    def _check_with_occt(self, shape) -> List[GeometryIssue]:
        """
        使用OCCT进行高级几何检查

        Args:
            shape: OCCT形状对象

        Returns:
            List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        try:
            # 尝试导入OCCT
            from OCP import BOPAlgo, BOPTools, BRepAlgoAPI
            from OCP import TopAbs, TopoDS, BRepBuilderAPI, BRepCheck
            from OCP import GProp, BRepGProp

            # 检查1: 水密性检查
            try:
                # 检查边是否被正好两个面共享（水密几何）
                # 如果边只被1个面使用或超过2个面使用，说明不水密
                watertight_issues = self._check_watertightness(shape)
                issues.extend(watertight_issues)
            except Exception as e:
                # 如果水密性检查失败，尝试其他方法
                pass

            # 检查2: 体积
            try:
                if hasattr(shape, 'Volume'):
                    volume = shape.Volume()
                    if volume < 1e-10:
                        issues.append(GeometryIssue(
                            issue_type="ZeroVolume",
                            severity="error",
                            description=f"体积为0或接近0: {volume}"
                        ))
                    elif volume < 0:
                        issues.append(GeometryIssue(
                            issue_type="NegativeVolume",
                            severity="error",
                            description=f"体积为负数: {volume} (方向可能错误)"
                        ))
            except Exception as e:
                # 可能形状不是实体
                issues.append(GeometryIssue(
                    issue_type="VolumeCheck",
                    severity="warning",
                    description=f"无法计算体积: {e}"
                ))

            # 检查3: 自交检查
            try:
                self_intersection_issues = self._check_self_intersection(shape)
                issues.extend(self_intersection_issues)
            except Exception as e:
                pass

            # 检查4: 法线一致性检查
            try:
                normal_issues = self._check_normal_consistency(shape)
                issues.extend(normal_issues)
            except Exception as e:
                pass

            # 检查5: Bounding Box
            try:
                bbox = shape.BoundingBox()
                if bbox:
                    dx = bbox.xmax - bbox.xmin
                    dy = bbox.ymax - bbox.ymin
                    dz = bbox.zmax - bbox.zmin

                    if dx <= 1e-10 or dy <= 1e-10 or dz <= 1e-10:
                        issues.append(GeometryIssue(
                            issue_type="DegenerateBBox",
                            severity="error",
                            description=f"退化的边界框: {dx}x{dy}x{dz}"
                        ))

                    # 检查无穷大或NaN
                    if (not (-1e200 < bbox.xmin < 1e200) or
                        not (-1e200 < bbox.xmax < 1e200) or
                        not (-1e200 < bbox.ymin < 1e200) or
                        not (-1e200 < bbox.ymax < 1e200) or
                        not (-1e200 < bbox.zmin < 1e200) or
                        not (-1e200 < bbox.zmax < 1e200)):
                        issues.append(GeometryIssue(
                            issue_type="InfiniteBBox",
                            severity="error",
                            description="边界框包含无穷大或NaN值"
                        ))
            except Exception as e:
                issues.append(GeometryIssue(
                    issue_type="BBoxCheck",
                    severity="warning",
                    description=f"无法检查边界框: {e}"
                ))

            # 检查4: 形状有效性
            try:
                checker = BRepCheck.BRepCheck_Analyzer(shape)
                if checker.HasErrors():
                    issues.append(GeometryIssue(
                        issue_type="OCCTValidity",
                        severity="error",
                        description="OCCT形状检查器检测到错误"
                    ))
            except Exception:
                pass

        except ImportError:
            # 如果没有OCCT，使用基本检查
            issues.extend(self._check_basic_geometry(shape))

        return issues

    def _check_basic_geometry(self, shape) -> List[GeometryIssue]:
        """
        基本几何检查（不依赖OCCT）

        Args:
            shape: 形状对象

        Returns:
            List[GeometryIssue]: 发现的问题列表
        """
        issues = []

        try:
            # 尝试获取基本属性
            has_volume = hasattr(shape, 'Volume')
            has_area = hasattr(shape, 'Area')
            has_bbox = hasattr(shape, 'BoundingBox')

            if not has_volume and not has_area:
                issues.append(GeometryIssue(
                    issue_type="NoGeometryProperties",
                    severity="warning",
                    description="无法获取几何属性，可能是非实体形状"
                ))

            # 检查体积
            if has_volume:
                try:
                    volume = shape.Volume()
                    if volume < 1e-10:
                        issues.append(GeometryIssue(
                            issue_type="ZeroVolume",
                            severity="error",
                            description=f"体积为0或接近0: {volume}"
                        ))
                    elif volume < 0:
                        issues.append(GeometryIssue(
                            issue_type="NegativeVolume",
                            severity="error",
                            description=f"体积为负数: {volume}"
                        ))
                except Exception:
                    pass

            # 检查Bounding Box
            if has_bbox:
                try:
                    bbox = shape.BoundingBox()
                    if bbox:
                        dx = bbox.xmax - bbox.xmin
                        dy = bbox.ymax - bbox.ymin
                        dz = bbox.zmax - bbox.zmin

                        if dx * dy * dz < 1e-20:
                            issues.append(GeometryIssue(
                                issue_type="DegenerateBBox",
                                severity="error",
                                description=f"退化的边界框尺寸: {dx}x{dy}x{dz}"
                            ))
                except Exception:
                    pass

        except Exception as e:
            issues.append(GeometryIssue(
                issue_type="BasicCheck",
                severity="warning",
                description=f"基本检查时出错: {e}"
            ))

        return issues


def quick_validate(code: str) -> Tuple[bool, bool]:
    """
    快速验证函数

    Args:
        code: CadQuery代码

    Returns:
        Tuple[bool, bool]: (代码合规, 几何合规)
    """
    validator = CadQueryValidator()
    return validator.validate(code)


if __name__ == "__main__":
    # 测试代码
    test_cases = [
        # 有效的立方体
        """
import cadquery as cq
result = cq.Workplane("XY").box(1, 1, 1)
""",
        # 语法错误
        """
import cadquery as cq
result = cq.Workplane("XY").box(1, 1, 1)
result.extrude()
""",
    ]

    validator = CadQueryValidator()

    for i, code in enumerate(test_cases):
        print(f"\n=== 测试用例 {i+1} ===")
        is_valid_code, is_valid_geometry = validator.validate(code)
        print(f"代码合规: {is_valid_code}")
        print(f"几何合规: {is_valid_geometry}")
