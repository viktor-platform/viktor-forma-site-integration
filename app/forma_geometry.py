import math
import random
import uuid
from dataclasses import dataclass

from .glb_bounds import Bounds3D


class GeometryValidationError(ValueError):
    """Raised when the block-generation inputs are inconsistent."""


class PlacementError(ValueError):
    """Raised when non-overlapping blocks cannot be placed in the requested area."""


@dataclass(frozen=True)
class GenerationSettings:
    block_count: int
    name_prefix: str
    clearance: float
    minimum_width: float
    maximum_width: float
    minimum_depth: float
    maximum_depth: float
    minimum_height: float
    maximum_height: float

    def validate(self) -> None:
        if not 1 <= self.block_count <= 50:
            raise GeometryValidationError("Block count must be between 1 and 50.")
        if not self.name_prefix.strip():
            raise GeometryValidationError("Block name prefix cannot be empty.")
        if self.clearance < 0:
            raise GeometryValidationError("Clearance cannot be negative.")
        if self.minimum_width <= 0 or self.maximum_width < self.minimum_width:
            raise GeometryValidationError("Width range is invalid.")
        if self.minimum_depth <= 0 or self.maximum_depth < self.minimum_depth:
            raise GeometryValidationError("Depth range is invalid.")
        if self.minimum_height <= 0 or self.maximum_height < self.minimum_height:
            raise GeometryValidationError("Height range is invalid.")


@dataclass(frozen=True)
class Block:
    index: int
    name: str
    center_x: float
    center_y: float
    width: float
    depth: float
    height: float
    elevation: float
    rotation_radians: float

    @property
    def rotation_degrees(self) -> float:
        return math.degrees(self.rotation_radians)


def generate_blocks_in_bounds(
    settings: GenerationSettings,
    bounds: Bounds3D,
    *,
    edge_margin: float,
    random_seed: str,
) -> list[Block]:
    """Generate a repeatable block layout within the active terrain bounds."""
    settings.validate()
    if edge_margin < 0:
        raise GeometryValidationError("Terrain edge margin cannot be negative.")

    rng = random.Random(random_seed)
    blocks: list[Block] = []
    for index in range(1, settings.block_count + 1):
        width = rng.uniform(settings.minimum_width, settings.maximum_width)
        depth = rng.uniform(settings.minimum_depth, settings.maximum_depth)
        height = rng.uniform(settings.minimum_height, settings.maximum_height)
        rotation = rng.uniform(0.0, 2.0 * math.pi)
        candidate_radius = 0.5 * math.hypot(width, depth)
        inset = edge_margin + candidate_radius
        minimum_x, maximum_x = bounds.minimum_x + inset, bounds.maximum_x - inset
        minimum_y, maximum_y = bounds.minimum_y + inset, bounds.maximum_y - inset
        if minimum_x >= maximum_x or minimum_y >= maximum_y:
            raise PlacementError(
                "Terrain bounds are too small for these blocks and margin."
            )

        for _ in range(2_000):
            center_x = rng.uniform(minimum_x, maximum_x)
            center_y = rng.uniform(minimum_y, maximum_y)
            if all(
                math.hypot(center_x - block.center_x, center_y - block.center_y)
                >= candidate_radius
                + 0.5 * math.hypot(block.width, block.depth)
                + settings.clearance
                for block in blocks
            ):
                blocks.append(
                    Block(
                        index=index,
                        name=f"{settings.name_prefix.strip()} {index:02d}",
                        center_x=center_x,
                        center_y=center_y,
                        width=width,
                        depth=depth,
                        height=height,
                        elevation=bounds.maximum_z,
                        rotation_radians=rotation,
                    )
                )
                break
        else:
            raise PlacementError(
                "The requested blocks do not fit inside the terrain bounds."
            )
    return blocks


def rectangle_ring(block: Block) -> list[list[float]]:
    """Return a closed clockwise XY ring for Forma's extrudedPolygon geometry."""
    half_width = block.width / 2.0
    half_depth = block.depth / 2.0
    local_points = [
        (-half_width, -half_depth),
        (-half_width, half_depth),
        (half_width, half_depth),
        (half_width, -half_depth),
    ]
    cosine = math.cos(block.rotation_radians)
    sine = math.sin(block.rotation_radians)
    points = [
        [
            round(block.center_x + local_x * cosine - local_y * sine, 6),
            round(block.center_y + local_x * sine + local_y * cosine, 6),
        ]
        for local_x, local_y in local_points
    ]
    area_twice = sum(
        point[0] * points[(index + 1) % len(points)][1]
        - point[1] * points[(index + 1) % len(points)][0]
        for index, point in enumerate(points)
    )
    if area_twice > 0:
        points.reverse()
    points.append(points[0].copy())
    return points


def to_basic_geometry_payload(block: Block) -> dict:
    """Convert a block to Forma's Basic Geometries batch-create schema."""
    return {
        "id": str(uuid.uuid4()),
        "name": block.name,
        "category": "building",
        "userData": {},
        "geometry": {
            "type": "extrudedPolygon",
            "coordinates": [rectangle_ring(block)],
            "height": round(block.height, 6),
            "elevation": round(block.elevation, 6),
        },
    }
