import json
import math
import struct
from dataclasses import dataclass
from itertools import product
from typing import Any


@dataclass(frozen=True)
class Bounds3D:
    minimum_x: float
    minimum_y: float
    minimum_z: float
    maximum_x: float
    maximum_y: float
    maximum_z: float


def terrain_bounds_from_glb(data: bytes) -> Bounds3D:
    document = _read_glb_json(data)
    accessors = document.get("accessors")
    meshes = document.get("meshes")
    nodes = document.get("nodes", [])
    if not isinstance(accessors, list) or not isinstance(meshes, list):
        raise ValueError("Terrain GLB does not contain accessors and meshes.")

    world_points: list[tuple[float, float, float]] = []
    referenced_meshes: set[int] = set()

    def visit(node_index: int, parent_matrix: list[float]) -> None:
        if not isinstance(nodes, list) or not 0 <= node_index < len(nodes):
            return
        node = nodes[node_index]
        if not isinstance(node, dict):
            return
        world_matrix = _matrix_multiply(parent_matrix, _node_matrix(node))
        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes):
            referenced_meshes.add(mesh_index)
            world_points.extend(
                _mesh_bounds_points(meshes[mesh_index], accessors, world_matrix)
            )
        children = node.get("children", [])
        if isinstance(children, list):
            for child_index in children:
                if isinstance(child_index, int):
                    visit(child_index, world_matrix)

    identity = _identity_matrix()
    scenes = document.get("scenes")
    scene_index = document.get("scene", 0)
    if (
        isinstance(scenes, list)
        and scenes
        and isinstance(scene_index, int)
        and 0 <= scene_index < len(scenes)
    ):
        root_nodes = scenes[scene_index].get("nodes", [])
    else:
        child_nodes = {
            child
            for node in nodes
            if isinstance(node, dict)
            for child in node.get("children", [])
            if isinstance(child, int)
        }
        root_nodes = [index for index in range(len(nodes)) if index not in child_nodes]
    for root_node in root_nodes:
        if isinstance(root_node, int):
            visit(root_node, identity)

    for mesh_index, mesh in enumerate(meshes):
        if mesh_index not in referenced_meshes:
            world_points.extend(_mesh_bounds_points(mesh, accessors, identity))

    if not world_points:
        raise ValueError("Terrain GLB has no POSITION accessors with min/max bounds.")
    # Terrain downloads are glTF/GLB and therefore Y-up. Forma element geometry
    # is authored Z-up, so map (glTF X, Y, Z) to (Forma X, Y, Z) as (X, Z, Y).
    return Bounds3D(
        minimum_x=min(point[0] for point in world_points),
        minimum_y=min(point[2] for point in world_points),
        minimum_z=min(point[1] for point in world_points),
        maximum_x=max(point[0] for point in world_points),
        maximum_y=max(point[2] for point in world_points),
        maximum_z=max(point[1] for point in world_points),
    )


def _read_glb_json(data: bytes) -> dict[str, Any]:
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("Terrain download is not a binary glTF (GLB) file.")
    version, total_length = struct.unpack_from("<II", data, 4)
    if version != 2 or total_length > len(data):
        raise ValueError("Terrain GLB header is invalid or unsupported.")
    offset = 12
    while offset + 8 <= total_length:
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk_data = data[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == 0x4E4F534A:
            payload = json.loads(chunk_data.rstrip(b"\x00 \t\r\n").decode("utf-8"))
            if isinstance(payload, dict):
                return payload
    raise ValueError("Terrain GLB does not contain a JSON chunk.")


def _mesh_bounds_points(
    mesh: Any, accessors: list[Any], matrix: list[float]
) -> list[tuple[float, float, float]]:
    if not isinstance(mesh, dict):
        return []
    points: list[tuple[float, float, float]] = []
    primitives = mesh.get("primitives", [])
    if not isinstance(primitives, list):
        return points
    for primitive in primitives:
        attributes = primitive.get("attributes") if isinstance(primitive, dict) else None
        accessor_index = attributes.get("POSITION") if isinstance(attributes, dict) else None
        if not isinstance(accessor_index, int) or not 0 <= accessor_index < len(accessors):
            continue
        accessor = accessors[accessor_index]
        minimum = accessor.get("min") if isinstance(accessor, dict) else None
        maximum = accessor.get("max") if isinstance(accessor, dict) else None
        if not (_is_vec3(minimum) and _is_vec3(maximum)):
            continue
        for x, y, z in product(
            (float(minimum[0]), float(maximum[0])),
            (float(minimum[1]), float(maximum[1])),
            (float(minimum[2]), float(maximum[2])),
        ):
            points.append(_transform_point(matrix, x, y, z))
    return points


def _is_vec3(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 3 and all(
        isinstance(item, (int, float)) for item in value[:3]
    )


def _identity_matrix() -> list[float]:
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _node_matrix(node: dict[str, Any]) -> list[float]:
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        return [float(value) for value in matrix]
    translation = node.get("translation", [0, 0, 0])
    scale = node.get("scale", [1, 1, 1])
    rotation = node.get("rotation", [0, 0, 0, 1])
    tx, ty, tz = (float(value) for value in translation[:3])
    sx, sy, sz = (float(value) for value in scale[:3])
    qx, qy, qz, qw = (float(value) for value in rotation[:4])
    length = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw) or 1.0
    qx, qy, qz, qw = qx / length, qy / length, qz / length, qw / length
    rotation_matrix = [
        1 - 2 * (qy * qy + qz * qz),
        2 * (qx * qy + qz * qw),
        2 * (qx * qz - qy * qw),
        0,
        2 * (qx * qy - qz * qw),
        1 - 2 * (qx * qx + qz * qz),
        2 * (qy * qz + qx * qw),
        0,
        2 * (qx * qz + qy * qw),
        2 * (qy * qz - qx * qw),
        1 - 2 * (qx * qx + qy * qy),
        0,
        tx,
        ty,
        tz,
        1,
    ]
    scale_matrix = [sx, 0, 0, 0, 0, sy, 0, 0, 0, 0, sz, 0, 0, 0, 0, 1]
    return _matrix_multiply(rotation_matrix, scale_matrix)


def _matrix_multiply(left: list[float], right: list[float]) -> list[float]:
    result = [0.0] * 16
    for column in range(4):
        for row in range(4):
            result[column * 4 + row] = sum(
                left[index * 4 + row] * right[column * 4 + index]
                for index in range(4)
            )
    return result


def _transform_point(matrix: list[float], x: float, y: float, z: float) -> tuple[float, float, float]:
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )
