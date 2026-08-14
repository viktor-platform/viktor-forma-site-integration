from dataclasses import dataclass
from io import BytesIO

import trimesh


@dataclass(frozen=True)
class Bounds3D:
    minimum_x: float
    minimum_y: float
    minimum_z: float
    maximum_x: float
    maximum_y: float
    maximum_z: float


def terrain_bounds_from_glb(data: bytes) -> Bounds3D:
    scene = trimesh.load_scene(BytesIO(data), file_type="glb")
    bounds = scene.bounds
    if bounds is None:
        raise ValueError("Terrain GLB has no geometry.")

    minimum, maximum = bounds
    # Terrain downloads are glTF/GLB and therefore Y-up. Forma element geometry
    # is authored Z-up, so map (glTF X, Y, Z) to (Forma X, Y, Z) as (X, Z, Y).
    return Bounds3D(
        minimum_x=float(minimum[0]),
        minimum_y=float(minimum[2]),
        minimum_z=float(minimum[1]),
        maximum_x=float(maximum[0]),
        maximum_y=float(maximum[2]),
        maximum_z=float(maximum[1]),
    )
