"""Parameter-set domain model.

The hierarchy follows the exposure-set concept used by LPBF build processors:

    ExposureSet  (layer thickness + an ordered list of up to four exposure types)
      └─ ExposureType  (Hatch, Core, Contour, Edge for parts; SupportHatch for supports)
           └─ Parameter (power, speed, hatch distance, offsets, angles, ...)

Every parameter carries UI metadata (label, unit, guard level, help text) in
``json_schema_extra`` so the GUI, CLI and exporters can be generated from the
model instead of being hand-written per field.

Units: lengths in mm, power in W, speed in mm/s, angles in degrees,
temperature in °C.
"""

from __future__ import annotations

import re
import secrets
from enum import Enum
from typing import Annotated, Any, ClassVar, Collection, Literal, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_serializer, model_validator

SCHEMA_VERSION = 1  # the format of new sets; format 2 is reached only by an explicit upgrade
PASS_ID_VERSION = 2  # the first format whose contour passes carry ids
# File-format versions this build reads and writes (docs/file_format.md). A set file without a
# schema_version key is read as version 1 (legacy policy); every other value is refused.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


class FileFormatError(ValueError):
    """A set file cannot be read as any supported format; it is refused, never repaired."""


class SchemaVersionError(FileFormatError):
    """A set file's schema_version is not one this build supports."""


class PassIdError(FileFormatError):
    """Contour-pass ids that do not fit the file's format."""


def check_schema_version(value: Any) -> int:
    """Return a supported schema_version, or raise SchemaVersionError explaining why not."""
    supported = ", ".join(map(str, SUPPORTED_SCHEMA_VERSIONS))
    if type(value) is not int or value < 1:  # bool, float, str, None, ... are not versions
        raise SchemaVersionError(
            f"schema_version {value!r} is invalid; it must be a whole number (supported: {supported})")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        plural = "s" if len(SUPPORTED_SCHEMA_VERSIONS) > 1 else ""
        raise SchemaVersionError(
            f"file format version {value} is not supported; this LPBF Studio reads version{plural} {supported} "
            f"(was the file written by a newer version?)")
    return value


# --------------------------------------------------------------------------- contour-pass ids
#
# A pass id is an opaque label that stays with its pass through edits and reordering. Ids are
# unique within one contour (so within one set); sets copied from one another share them on
# purpose. LPBF Studio generates 8 random hex characters and never derives an id from a
# position, a name or a value.

_PASS_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
PASS_ID_RULE = "ids are 1 to 32 characters a-z, 0-9, '_' or '-', starting with a letter or digit"


def is_valid_pass_id(value: Any) -> bool:
    return type(value) is str and _PASS_ID.fullmatch(value) is not None


def new_pass_id(taken: Collection[str] = ()) -> str:
    """A fresh random pass id that is not in ``taken``."""
    while True:
        pid = secrets.token_hex(4)
        if pid not in taken:
            return pid


def describe_pass(position: int, name: Any) -> str:
    """``contour pass #2 'Contour 2'`` for the pass at 0-based ``position``."""
    return f"contour pass #{position + 1} {name!r}" if isinstance(name, str) else f"contour pass #{position + 1}"


def pass_id_problem(passes: Sequence[tuple[bool, Any, Any]], version: int | None) -> str | None:
    """Why the pass ids of one contour do not fit ``version``, or None.

    ``passes`` holds ``(has_id, id, name)`` per pass in order. ``version`` None checks only that
    ids are given for every pass or none, are valid and are unique (the in-memory rule)."""
    with_id = [i for i, (has, _, _) in enumerate(passes) if has]
    if version is not None and version < PASS_ID_VERSION:
        if with_id:
            i = with_id[0]
            return (f"{describe_pass(i, passes[i][2])} has an id, which format {version} files cannot hold; "
                    f"the file is refused rather than guessed as format {PASS_ID_VERSION}")
        return None
    if version is not None or with_id:
        for i, (has, value, name) in enumerate(passes):
            if not has or value is None:
                if version is None:
                    return f"contour passes need an id on every pass or none: {describe_pass(i, name)} has no id"
                return f"{describe_pass(i, name)} has no id; format {version} files need an id on every pass"
    first: dict[str, int] = {}
    for i, (has, value, name) in enumerate(passes):
        if not has:
            continue
        if not is_valid_pass_id(value):
            return f"{describe_pass(i, name)}: id {value!r} is invalid; {PASS_ID_RULE}"
        if value in first:
            return (f"contour passes #{first[value] + 1} and #{i + 1} share the id {value!r}; "
                    f"ids must be unique within the contour")
        first[value] = i
    return None
MAX_EXPOSURE_TYPES = 4


class Guard(str, Enum):
    """How carefully a parameter should be changed.

    FREE      - safe to experiment with.
    CAUTION   - better know what it does (affects quality / build time).
    CRITICAL  - must know what it does (affects process stability / machine safety).
    """

    FREE = "free"
    CAUTION = "caution"
    CRITICAL = "critical"


def param(
    default: Any,
    *,
    label: str,
    unit: str | None = None,
    ge: float | None = None,
    le: float | None = None,
    guard: Guard = Guard.FREE,
    help: str = "",
    decimals: int | None = None,
) -> Any:
    extra: dict[str, Any] = {"label": label, "guard": guard.value, "help": help}
    if unit:
        extra["unit"] = unit
    if ge is not None:
        extra["min"] = ge
    if le is not None:
        extra["max"] = le
    if decimals is not None:
        extra["decimals"] = decimals
    return Field(default, ge=ge, le=le, description=help or label, json_schema_extra=extra)


def power(default: float, label: str = "Laser power", guard: Guard = Guard.CAUTION) -> Any:
    return param(default, label=label, unit="W", ge=0, le=2000, guard=guard, decimals=1,
                 help="Nominal laser power for this exposure.")


def speed(default: float, label: str = "Scan speed", guard: Guard = Guard.CAUTION) -> Any:
    return param(default, label=label, unit="mm/s", ge=1, le=20000, guard=guard, decimals=0,
                 help="Scanner speed along the vector.")


def hatch_distance(default: float) -> Any:
    return param(default, label="Hatch distance", unit="mm", ge=0.01, le=5, guard=Guard.CAUTION, decimals=3,
                 help="Spacing between neighbouring hatch vectors. Larger values save time but increase porosity.")


def enabled(default: bool = True) -> Any:
    return param(default, label="Enabled", help="Switch this exposure on or off.")


class Node(BaseModel):
    """Base for every editable node. Assignments are validated."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    node_label: ClassVar[str] = ""

    def label(self) -> str:
        return self.node_label or type(self).__name__


# --------------------------------------------------------------------------- enums


class HatchPattern(str, Enum):
    STRIPES = "stripes"
    CHESS = "chess"
    NONE = "none"


class Homogenization(str, Enum):
    NONE = "none"
    TIME = "time"
    POWER_REDUCTION = "power_reduction"


class ExposureMode(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"


class SupportPattern(str, Enum):
    UNIDIRECTIONAL = "unidirectional"
    BIDIRECTIONAL = "bidirectional"


class BaseRadiusMethod(str, Enum):
    QUARTER_CIRCLE = "quarter_circle"
    MATTHECK = "mattheck"


class SkinPriority(str, Enum):
    DOWNSKIN = "downskin"
    UPSKIN = "upskin"


class SetCategory(str, Enum):
    PART = "part"
    SUPPORT = "support"


class LayerExposureOrder(str, Enum):
    PART = "part"    # each part fully exposed (all exposure types) before the next part
    LAYER = "layer"  # each exposure type exposed for all parts before the next type


# --------------------------------------------------------------------------- hatch


class StripeSettings(Node):
    node_label: ClassVar[str] = "Stripes"
    stripe_width_mm: float = param(5.0, label="Stripe width", unit="mm", ge=0.5, le=100, guard=Guard.CAUTION,
                                   decimals=2, help="Nominal hatch vector length. Wider stripes keep more heat in the scan area.")
    stripe_overlap_mm: float = param(0.08, label="Stripe overlap", unit="mm", ge=0, le=2, guard=Guard.CAUTION,
                                     decimals=3, help="Overlap between neighbouring stripes; too little leaves lack-of-fusion at borders.")
    shifted: bool = param(True, label="Shift stripes per layer",
                          help="Shift stripe borders from layer to layer so they do not stack.")


class ChessSettings(Node):
    node_label: ClassVar[str] = "Chess"
    square_width_mm: float = param(5.0, label="Square width", unit="mm", ge=0.5, le=50, guard=Guard.CAUTION, decimals=2)
    gap_width_mm: float = param(0.1, label="Gap width", unit="mm", ge=0, le=5, guard=Guard.CAUTION, decimals=3,
                                help="Gaps between squares are exposed like contours after the squares.")
    square_gap_overlap_mm: float = param(0.03, label="Square/gap overlap", unit="mm", ge=0, le=1, guard=Guard.CAUTION, decimals=3)
    gap_power_w: float = power(150.0, "Gap laser power")
    gap_speed_mm_s: float = speed(800.0, "Gap scan speed")


class InFill(Node):
    node_label: ClassVar[str] = "InFill"
    enabled: bool = enabled()
    power_w: float = power(285.0)
    speed_mm_s: float = speed(960.0)
    hatch_distance_mm: float = hatch_distance(0.11)
    pattern: HatchPattern = param(HatchPattern.STRIPES, label="Exposure pattern", guard=Guard.CAUTION)
    stripes: StripeSettings = Field(default_factory=StripeSettings)
    chess: ChessSettings = Field(default_factory=ChessSettings)
    offset_angle_deg: float = param(0.0, label="Offset angle", unit="°", ge=0, le=360, decimals=1,
                                    help="Hatch direction in the first layer.")
    rotation_angle_deg: float = param(67.0, label="Rotation angle", unit="°", ge=0, le=360, decimals=1,
                                      help="Rotation of the hatch direction from one layer to the next.")
    restriction_angle_deg: float = param(0.0, label="Restriction angle", unit="°", ge=0, le=90, guard=Guard.CAUTION, decimals=1,
                                         help="Half-width of the corridor around the gas-flow axis in which hatch directions are skipped. 0 = all directions valid.")
    skip_layers: int = param(0, label="InFill skipping", unit="layers", ge=0, le=10, guard=Guard.CAUTION,
                             help="Expose infill only every (n+1)-th layer. Up/downskin and contours are still exposed every layer.")
    skywriting: bool = param(True, label="Skywriting", guard=Guard.CRITICAL,
                             help="Accelerate/decelerate the scanner outside the vector for constant energy input. Do not switch off without a reason.")
    flow_optimization: bool = param(True, label="Flow optimization", guard=Guard.CAUTION,
                                    help="Expose stripes against the gas flow, unidirectionally.")
    exposure_mode: ExposureMode = param(ExposureMode.SINGLE, label="Exposure mode", guard=Guard.CAUTION,
                                        help="Double: expose the layer a second time with the pattern rotated by 90°.")
    homogenization: Homogenization = param(Homogenization.NONE, label="Energy input homogenization", guard=Guard.CAUTION,
                                           help="How short vectors are compensated: travel full stripe length (time) or reduce power.")
    power_reduction_factor: float = param(0.0, label="Power reduction factor", ge=0, le=1, guard=Guard.CRITICAL, decimals=2,
                                          help="r in P(h) = P0 - r·P0·(1 - h/h0). Only used with power-reduction homogenization.")


class Skin(Node):
    enabled: bool = enabled()
    power_w: float = power(250.0)
    speed_mm_s: float = speed(800.0)
    hatch_distance_mm: float = hatch_distance(0.10)
    thickness_mm: float = param(0.12, label="Skin thickness", unit="mm", ge=0, le=2, guard=Guard.CAUTION, decimals=3,
                                help="How deep the skin reaches. Should be a multiple of the layer thickness.")
    min_length_mm: float = param(0.2, label="Minimum length", unit="mm", ge=0, le=10, guard=Guard.CAUTION, decimals=3,
                                 help="Skin areas narrower than this are exposed as infill instead.")
    ridge_mm: float = param(0.0, label="Ridge", unit="mm", ge=-1, le=1, guard=Guard.CRITICAL, decimals=3,
                            help="Negative: skip skin on steep overhangs. Positive: even vertical walls are treated as skin.")
    overlap_with_infill_mm: float = param(0.05, label="Overlap with infill", unit="mm", ge=0, le=2, decimals=3)
    time_optimized: bool = param(True, label="Time optimized", help="Join skin vectors with infill vectors where possible.")
    skywriting: bool = param(True, label="Skywriting", guard=Guard.CRITICAL)


class UpSkin(Skin):
    node_label: ClassVar[str] = "UpSkin"


class DownSkin(Skin):
    node_label: ClassVar[str] = "DownSkin"
    power_w: float = power(150.0)
    speed_mm_s: float = speed(1000.0)
    hatch_distance_mm: float = hatch_distance(0.09)


class Hatch(Node):
    node_label: ClassVar[str] = "Hatch"
    kind: Literal["hatch"] = "hatch"
    hatch_offset_mm: float = param(0.015, label="Hatch offset", unit="mm", ge=-1, le=1, guard=Guard.CAUTION, decimals=3,
                                   help="Distance of the hatch from the global beam offset line.")
    skin_priority: SkinPriority = param(SkinPriority.DOWNSKIN, label="Skin priority",
                                        help="Which skin wins where up- and downskin coincide in one layer.")
    infill: InFill = Field(default_factory=InFill)
    upskin: UpSkin = Field(default_factory=UpSkin)
    downskin: DownSkin = Field(default_factory=DownSkin)


# --------------------------------------------------------------------------- core


class Core(Node):
    node_label: ClassVar[str] = "Core"
    kind: Literal["core"] = "core"
    enabled: bool = enabled()
    skin_thickness_xy_mm: float = param(2.0, label="Skin thickness x/y", unit="mm", ge=0, le=50, guard=Guard.CAUTION, decimals=2,
                                        help="Wall thickness exposed with the normal hatch before the core starts.")
    skin_thickness_z_mm: float = param(2.0, label="Skin thickness z", unit="mm", ge=0, le=50, guard=Guard.CAUTION, decimals=2)
    open_to_platform: bool = param(False, label="Open to platform", help="Let the core reach down to the build platform.")
    base_radius_mm: float = param(0.0, label="Base radius", unit="mm", ge=0, le=50, decimals=2)
    base_radius_method: BaseRadiusMethod = param(BaseRadiusMethod.QUARTER_CIRCLE, label="Base radius method",
                                                 help="Quarter circle, or Mattheck tensile-triangle radius for better stress distribution.")
    power_w: float = power(370.0)
    speed_mm_s: float = speed(1300.0)
    hatch_distance_mm: float = hatch_distance(0.19)
    pattern: HatchPattern = param(HatchPattern.CHESS, label="Exposure pattern", guard=Guard.CAUTION)
    chess: ChessSettings = Field(default_factory=ChessSettings)
    stripes: StripeSettings = Field(default_factory=StripeSettings)
    offset_angle_deg: float = param(0.0, label="Offset angle", unit="°", ge=0, le=360, decimals=1)
    rotation_angle_deg: float = param(67.0, label="Rotation angle", unit="°", ge=0, le=360, decimals=1)


# --------------------------------------------------------------------------- contour


class ContourPass(Node):
    node_label: ClassVar[str] = "Contour pass"
    # format 2 only, serialized first; None in format 1 sets, whose files carry no id key
    id: str | None = Field(None, frozen=True, description="Pass ID", json_schema_extra={
        "label": "Pass ID", "guard": "free",
        "help": "Identifies this pass through edits and reordering. It cannot be changed."})
    name: str = param("Contour 1", label="Name")
    enabled: bool = enabled()
    offset_mm: float = param(0.02, label="Contour offset", unit="mm", ge=-1, le=1, guard=Guard.CAUTION, decimals=3,
                             help="Offset of this contour from the global beam offset line (positive = inwards).")
    power_w: float = power(150.0, "Laser power (in-skin)")
    speed_mm_s: float = speed(600.0, "Scan speed (in-skin)")
    upskin_power_w: float = power(150.0, "Laser power (upskin)")
    upskin_speed_mm_s: float = speed(600.0, "Scan speed (upskin)")
    downskin_power_w: float = power(80.0, "Laser power (downskin)")
    downskin_speed_mm_s: float = speed(800.0, "Scan speed (downskin)")
    # the id this pass was read or created with; the library refuses to write a different one
    _origin_id: str | None = PrivateAttr(None)

    def model_post_init(self, context: Any) -> None:
        self._origin_id = self.id

    @field_validator("id", mode="before")
    @classmethod
    def _check_id(cls, value: Any) -> Any:
        if value is not None and not is_valid_pass_id(value):
            raise ValueError(f"id {value!r} is invalid; {PASS_ID_RULE}")
        return value

    @model_serializer(mode="wrap")
    def _omit_missing_id(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("id") is None:
            data.pop("id", None)  # format 1 files have no id key, exactly as before format 2
        return data

    def label(self) -> str:
        return self.name


def _default_passes() -> list[ContourPass]:
    return [
        ContourPass(name="Contour 1", offset_mm=0.06, power_w=150, speed_mm_s=600),
        ContourPass(name="Contour 2", offset_mm=0.02, power_w=80, speed_mm_s=900,
                    upskin_power_w=80, upskin_speed_mm_s=900, downskin_power_w=60, downskin_speed_mm_s=1000),
    ]


class Contour(Node):
    node_label: ClassVar[str] = "Contour"
    kind: Literal["contour"] = "contour"
    upskin_corridor_mm: float = param(0.1, label="Upskin corridor", unit="mm", ge=0, le=5, guard=Guard.CAUTION, decimals=3,
                                      help="Together with thickness, sets the angle below which contours count as upskin.")
    upskin_thickness_mm: float = param(0.12, label="Upskin thickness", unit="mm", ge=0, le=5, guard=Guard.CAUTION, decimals=3)
    downskin_corridor_mm: float = param(0.1, label="Downskin corridor", unit="mm", ge=0, le=5, guard=Guard.CAUTION, decimals=3)
    downskin_thickness_mm: float = param(0.12, label="Downskin thickness", unit="mm", ge=0, le=5, guard=Guard.CAUTION, decimals=3)
    passes: list[ContourPass] = Field(default_factory=_default_passes, min_length=1, max_length=4)

    @model_validator(mode="after")
    def _check_pass_ids(self) -> "Contour":
        problem = pass_id_problem([(p.id is not None, p.id, p.name) for p in self.passes], None)
        if problem:
            raise ValueError(problem)
        return self


# --------------------------------------------------------------------------- edge


class Edge(Node):
    node_label: ClassVar[str] = "Edge"
    kind: Literal["edge"] = "edge"
    enabled: bool = enabled()
    power_w: float = power(100.0)
    speed_mm_s: float = speed(800.0)
    edge_offset_mm: float = param(0.02, label="Edge offset", unit="mm", ge=-1, le=1, guard=Guard.CAUTION, decimals=3,
                                  help="Added to global and part beam offset: TBO = GBO + PBO + EO.")
    method1_enabled: bool = param(True, label="Method 1: edge factor / threshold",
                                  help="Edges at part peaks (type 1).")
    edge_factor: float = param(2.0, label="Edge factor", ge=0, le=10, guard=Guard.CAUTION, decimals=2,
                               help="Distance from contour intersection to edge end = factor × TBO.")
    threshold: float = param(2.0, label="Threshold", ge=0, le=20, guard=Guard.CAUTION, decimals=2,
                             help="Peaks longer than threshold × TBO use the minimum edge factor (1.45).")
    method2_enabled: bool = param(True, label="Method 2: minimum radius",
                                  help="Edges at peaks (type 1) and thin walls / bridges (type 2).")
    min_radius_factor: float = param(0.5, label="Min. radius factor", ge=0, le=1, guard=Guard.CAUTION, decimals=2,
                                     help="Adds an edge where peak width > factor × 2 × TBO.")


# --------------------------------------------------------------------------- support


class SupportHatch(Node):
    node_label: ClassVar[str] = "Support"
    kind: Literal["support"] = "support"
    enabled: bool = enabled()
    power_w: float = power(200.0)
    speed_mm_s: float = speed(900.0)
    pattern: SupportPattern = param(SupportPattern.BIDIRECTIONAL, label="Exposure pattern",
                                    help="Bidirectional saves jump time; unidirectional exposes every line in the same direction.")


ExposureType = Annotated[Union[Hatch, Core, Contour, Edge, SupportHatch], Field(discriminator="kind")]

EXPOSURE_CLASSES: dict[str, type[Node]] = {
    "hatch": Hatch, "core": Core, "contour": Contour, "edge": Edge, "support": SupportHatch,
}
PART_KINDS = ("hatch", "core", "contour", "edge")
SUPPORT_KINDS = ("support",)


def allowed_kinds(category: SetCategory) -> tuple[str, ...]:
    return SUPPORT_KINDS if category == SetCategory.SUPPORT else PART_KINDS


# --------------------------------------------------------------------------- exposure set


class ExposureSet(Node):
    node_label: ClassVar[str] = "Exposure set"
    schema_version: int = Field(SCHEMA_VERSION, frozen=True)
    name: str = param("New_Part", label="Name")
    category: SetCategory = param(SetCategory.PART, label="Category")
    description: str = param("", label="Description")
    material: str = param("", label="Material")
    machine: str = param("", label="Machine profile")
    locked: bool = param(False, label="Locked", help="Locked reference sets cannot be edited; copy them instead.")
    color: str = param("#2a9d8f", label="Color")
    layer_thickness_mm: float = param(0.04, label="Layer thickness", unit="mm", ge=0.01, le=0.2, guard=Guard.CRITICAL, decimals=3,
                                      help="Distance between exposed layers. Changes almost every other parameter's effect.")
    exposure_types: list[ExposureType] = Field(default_factory=list)
    # the version this set was read or created with; the library refuses to write a different one
    _origin_version: int = PrivateAttr(SCHEMA_VERSION)

    def model_post_init(self, context: Any) -> None:
        self._origin_version = self.schema_version

    @field_validator("schema_version", mode="before")
    @classmethod
    def _check_schema_version(cls, value: Any) -> int:
        return check_schema_version(value)

    @model_validator(mode="after")
    def _check_types(self) -> "ExposureSet":
        kinds = [et.kind for et in self.exposure_types]
        if len(kinds) > MAX_EXPOSURE_TYPES:
            raise ValueError(f"an exposure set holds at most {MAX_EXPOSURE_TYPES} exposure types")
        if len(set(kinds)) != len(kinds):
            raise ValueError("each exposure type may appear only once in a set")
        allowed = allowed_kinds(self.category)
        bad = [k for k in kinds if k not in allowed]
        if bad:
            raise ValueError(f"{self.category.value} sets cannot contain {', '.join(bad)}")
        return self

    @model_validator(mode="after")
    def _check_format(self) -> "ExposureSet":
        contour = self.get("contour")
        if isinstance(contour, Contour):
            problem = pass_id_problem([(p.id is not None, p.id, p.name) for p in contour.passes],
                                      self.schema_version)
            if problem:
                raise ValueError(problem)
        return self

    def get(self, kind: str) -> Node | None:
        return next((et for et in self.exposure_types if et.kind == kind), None)

    def kinds(self) -> list[str]:
        return [et.kind for et in self.exposure_types]


def identity_problem(es: ExposureSet) -> str | None:
    """Why ``es`` must not be written: its schema_version or a contour pass id differs from the
    value it was read or created with. Both fields are frozen, so only a validation bypass
    (``model_copy(update=...)``, ``__dict__``, ``object.__setattr__``) can change them."""
    if es.schema_version != es._origin_version:
        return (f"its schema_version was changed from {es._origin_version!r} to {es.schema_version!r} "
                "after it was read; the file format changes only through an explicit upgrade")
    contour = es.get("contour")
    for position, p in enumerate(contour.passes if isinstance(contour, Contour) else (), start=1):
        if p.id != p._origin_id:
            return (f"the id of contour pass #{position} {p.name!r} was changed from {p._origin_id!r} "
                    f"to {p.id!r} after it was read; a contour pass keeps its id for life")
    return None


# --------------------------------------------------------------------------- job / machine


class ProcessSettings(Node):
    """Job-level settings that apply on top of exposure sets."""

    node_label: ClassVar[str] = "Process settings"
    global_beam_offset_mm: float = param(0.04, label="Global beam offset (GBO)", unit="mm", ge=-1, le=1, guard=Guard.CRITICAL,
                                         decimals=3, help="Compensates melt-pool width. Positive moves the offset inside the nominal contour.")
    scaling_x_pct: float = param(100.0, label="Global scaling X", unit="%", ge=90, le=110, guard=Guard.CRITICAL, decimals=3,
                                 help="Compensates material shrinkage on cooling.")
    scaling_y_pct: float = param(100.0, label="Global scaling Y", unit="%", ge=90, le=110, guard=Guard.CRITICAL, decimals=3)
    platform_temperature_c: float = param(35.0, label="Platform temperature", unit="°C", ge=20, le=500, guard=Guard.CAUTION, decimals=0)
    exposure_order: LayerExposureOrder = param(LayerExposureOrder.PART, label="Exposure order mode",
                                               help="Part: finish each part before the next. Layer: each exposure type across all parts.")
    sort_against_flow: bool = param(True, label="Sort parts against gas flow",
                                    help="Expose parts downstream first so spatter and smoke do not land on fresh layers.")
    automatic_dosing: bool = param(True, label="Automatic dosing")
    machine_dosing_factor_pct: float = param(100.0, label="Machine dosing factor", unit="%", ge=50, le=400, guard=Guard.CAUTION,
                                             decimals=0, help="Multiplied with the automatic dosing factor.")
    dosing_boost_pct: float = param(150.0, label="Dosing boost", unit="%", ge=100, le=400, decimals=0,
                                    help="Extra powder for the first layers (uneven platform, powder gaps).")
    dosing_boost_layers: int = param(10, label="Dosing boost layers", unit="layers", ge=0, le=500)
    recoat_time_s: float = param(9.0, label="Recoat time per layer", unit="s", ge=0, le=120, decimals=1,
                                 help="Used for build-time estimates only.")


class MachineProfile(Node):
    node_label: ClassVar[str] = "Machine profile"
    name: str = param("Generic 400 W", label="Name")
    max_power_w: float = param(400.0, label="Max laser power", unit="W", ge=1, le=5000, decimals=0)
    max_speed_mm_s: float = param(7000.0, label="Max scan speed", unit="mm/s", ge=1, le=30000, decimals=0)
    min_layer_mm: float = param(0.02, label="Min layer thickness", unit="mm", ge=0.005, le=0.5, decimals=3)
    max_layer_mm: float = param(0.10, label="Max layer thickness", unit="mm", ge=0.005, le=0.5, decimals=3)
    max_platform_temperature_c: float = param(200.0, label="Max platform temperature", unit="°C", ge=20, le=1000, decimals=0)
    build_x_mm: float = param(250.0, label="Build area X", unit="mm", ge=1, le=2000, decimals=0)
    build_y_mm: float = param(250.0, label="Build area Y", unit="mm", ge=1, le=2000, decimals=0)
    build_z_mm: float = param(325.0, label="Build height", unit="mm", ge=1, le=2000, decimals=0)
    energy_window_j_mm3: tuple[float, float] = Field((30.0, 150.0), description="Typical volumetric energy window for warnings")
