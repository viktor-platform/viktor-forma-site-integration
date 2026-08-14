import viktor as vkt

from forma_api import FormaClient, parse_proposal_urn
from forma_geometry import (
    GenerationSettings,
    GeometryValidationError,
    PlacementError,
    generate_blocks_in_bounds,
    to_basic_geometry_payload,
)
from glb_bounds import Bounds3D, terrain_bounds_from_glb


APS_INTEGRATION_NAME = "forma-site"


class Parametrization(vkt.Parametrization):
    destination = vkt.Section("Forma destination", initially_expanded=True)
    destination.help = vkt.Text(
        "Enter the Forma Site Design project/site ID and a full proposal URN. "
        "Use the **Forma proposals** view to list the current proposal URNs."
    )
    destination.project_id = vkt.TextField(
        "Forma project/site ID",
        description="The auth context, usually beginning with pro_.",
        flex=100,
    )
    destination.proposal_urn = vkt.TextField(
        "Full proposal URN",
        description=(
            "Example shape: urn:adsk-forma-elements:proposal:project-id:"
            "proposal-id:revision-id"
        ),
        flex=100,
    )
    destination.region = vkt.OptionField(
        "Forma region", options=["US", "EMEA"], default="US", flex=50
    )
    destination.terrain_edge_margin = vkt.NumberField(
        "Terrain edge margin",
        default=20.0,
        min=0.0,
        suffix="m",
        num_decimals=1,
        flex=50,
    )

    generation = vkt.Section("Block generation", initially_expanded=True)
    generation.block_count = vkt.IntegerField(
        "Number of blocks", default=5, min=1, max=50, step=1, flex=50
    )
    generation.name_prefix = vkt.TextField(
        "Block name prefix", default="VIKTOR Block", flex=100
    )
    generation.clearance = vkt.NumberField(
        "Minimum clearance",
        default=4.0,
        min=0.0,
        suffix="m",
        num_decimals=2,
        flex=50,
    )
    dimensions = vkt.Section("Random dimensions", initially_expanded=True)
    dimensions.minimum_width = vkt.NumberField(
        "Minimum width", default=15.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )
    dimensions.maximum_width = vkt.NumberField(
        "Maximum width", default=30.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )
    dimensions.minimum_depth = vkt.NumberField(
        "Minimum depth", default=12.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )
    dimensions.maximum_depth = vkt.NumberField(
        "Maximum depth", default=24.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )
    dimensions.minimum_height = vkt.NumberField(
        "Minimum height", default=15.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )
    dimensions.maximum_height = vkt.NumberField(
        "Maximum height", default=60.0, min=0.1, suffix="m", num_decimals=2, flex=50
    )

    publish = vkt.Section("Publish", initially_expanded=True)
    publish.warning = vkt.Text(
        "The button creates Forma geometry elements and then writes a new proposal "
        "revision that preserves the current terrain, base and existing children."
    )
    publish.push = vkt.ActionButton(
        "Push blocks to Forma",
        method="push_blocks",
        longpoll=True,
        flex=100,
        description=(
            "Create a new random block layout and attach it to the selected proposal."
        ),
    )


class Controller(vkt.Controller):
    label = "Forma block scene"
    parametrization = Parametrization(width=42)

    @vkt.GeometryView(
        "3D preview",
        x_axis_to_right=True,
        default_shadow=True,
        description=(
            "Random local-coordinate preview of building blocks."
        ),
    )
    def geometry_preview(self, params, **kwargs):
        project_id = str(params.destination.project_id or "").strip()
        proposal_urn = str(params.destination.proposal_urn or "").strip()
        if not project_id or not proposal_urn:
            return vkt.GeometryResult(vkt.Group([]))
        client = FormaClient(
            vkt.external.OAuth2Integration(APS_INTEGRATION_NAME).get_access_token(),
            region=str(params.destination.region),
        )
        terrain_urn = client.get_proposal_terrain_urn(project_id, proposal_urn)
        terrain_bounds = terrain_bounds_from_glb(
            client.download_terrain_glb(project_id, terrain_urn)
        )
        blocks = _generate_from_params(params, terrain_bounds)

        building_material = vkt.Material(
            "Buildings", color=vkt.Color(88, 151, 214)
        )
        objects = []
        labels = []

        for block in blocks:
            box = vkt.RectangularExtrusion(
                block.width,
                block.depth,
                line=vkt.Line(
                    vkt.Point(0, 0, 0), vkt.Point(0, 0, block.height)
                ),
                material=building_material,
            )
            box.rotate(block.rotation_radians, direction=[0, 0, 1])
            box.translate([block.center_x, block.center_y, block.elevation])
            objects.append(box)
            labels.append(
                vkt.Label(
                    vkt.Point(
                        block.center_x,
                        block.center_y,
                        block.elevation + block.height,
                    ),
                    block.name,
                )
            )

        return vkt.GeometryResult(vkt.Group(objects), labels=labels)

    @vkt.TableView("Block schedule")
    def block_schedule(self, params, **kwargs):
        project_id = str(params.destination.project_id or "").strip()
        proposal_urn = str(params.destination.proposal_urn or "").strip()
        if not project_id or not proposal_urn:
            blocks = []
        else:
            client = FormaClient(
                vkt.external.OAuth2Integration(APS_INTEGRATION_NAME).get_access_token(),
                region=str(params.destination.region),
            )
            terrain_urn = client.get_proposal_terrain_urn(project_id, proposal_urn)
            terrain_bounds = terrain_bounds_from_glb(
                client.download_terrain_glb(project_id, terrain_urn)
            )
            blocks = _generate_from_params(params, terrain_bounds)
        data = [
            [
                block.name,
                round(block.center_x, 2),
                round(block.center_y, 2),
                round(block.width, 2),
                round(block.depth, 2),
                round(block.height, 2),
                round(block.rotation_degrees, 1),
            ]
            for block in blocks
        ]
        return vkt.TableResult(
            data,
            column_headers=[
                "Name",
                "X [m]",
                "Y [m]",
                "Width [m]",
                "Depth [m]",
                "Height [m]",
                "Rotation [deg]",
            ],
        )

    @vkt.TableView(
        "Forma proposals",
        duration_guess=5,
        update_label="Load proposals",
        description="Lists proposal URNs for the entered project/site ID.",
    )
    def forma_proposals(self, params, **kwargs):
        project_id = str(params.destination.project_id or "").strip()
        if not project_id:
            raise vkt.UserError(
                "Enter a Forma project/site ID before loading proposals."
            )
        client = FormaClient(
            vkt.external.OAuth2Integration(APS_INTEGRATION_NAME).get_access_token(),
            region=str(params.destination.region),
        )
        proposals = client.list_proposals(project_id, limit=20)

        data = [
            [
                proposal.display_name,
                proposal.proposal_id,
                proposal.revision_id,
                proposal.urn,
            ]
            for proposal in proposals
        ]
        return vkt.TableResult(
            data,
            column_headers=["Proposal", "Proposal ID", "Revision ID", "Proposal URN"],
        )

    def push_blocks(self, params, **kwargs):
        project_id = str(params.destination.project_id or "").strip()
        if not project_id:
            raise vkt.UserError("Enter the Forma project/site ID before publishing.")
        proposal_urn = str(params.destination.proposal_urn or "").strip()
        if not proposal_urn:
            raise vkt.UserError("Enter a full proposal URN before publishing.")

        try:
            proposal_parts = parse_proposal_urn(proposal_urn)
        except ValueError as exc:
            raise vkt.UserError(str(exc)) from exc
        if proposal_parts.project_id != project_id:
            raise vkt.UserError(
                "The project/site ID does not match the project ID contained in the "
                "proposal URN."
            )

        client = FormaClient(
            vkt.external.OAuth2Integration(APS_INTEGRATION_NAME).get_access_token(),
            region=str(params.destination.region),
        )
        vkt.progress_message(
            message="Reading the active Forma terrain bounds...", percentage=10
        )
        terrain_urn = client.get_proposal_terrain_urn(project_id, proposal_urn)
        terrain_bounds = terrain_bounds_from_glb(
            client.download_terrain_glb(project_id, terrain_urn)
        )
        blocks = _generate_from_params(params, terrain_bounds)
        payloads = [to_basic_geometry_payload(block) for block in blocks]
        vkt.progress_message(
            message=f"Creating {len(payloads)} Forma geometry elements...",
            percentage=20,
        )
        created_urns = client.create_basic_geometries(project_id, payloads)
        vkt.progress_message(
            message="Creating a new proposal revision with the blocks attached...",
            percentage=70,
        )
        updated_proposal_urn = client.attach_elements_to_proposal(
            project_id=project_id,
            proposal_urn=proposal_urn,
            element_urns=created_urns,
        )

        message = f"Created and attached {len(created_urns)} blocks."
        if updated_proposal_urn:
            message += f" New proposal revision: {updated_proposal_urn}"
        vkt.progress_message(message="Forma update completed.", percentage=100)
        vkt.UserMessage.success(message)


def _generate_from_params(params, terrain_bounds: Bounds3D):
    settings = GenerationSettings(
        block_count=int(params.generation.block_count),
        name_prefix=str(params.generation.name_prefix or ""),
        clearance=float(params.generation.clearance),
        minimum_width=float(params.dimensions.minimum_width),
        maximum_width=float(params.dimensions.maximum_width),
        minimum_depth=float(params.dimensions.minimum_depth),
        maximum_depth=float(params.dimensions.maximum_depth),
        minimum_height=float(params.dimensions.minimum_height),
        maximum_height=float(params.dimensions.maximum_height),
    )
    try:
        return generate_blocks_in_bounds(
            settings,
            terrain_bounds,
            edge_margin=float(params.destination.terrain_edge_margin),
        )
    except (GeometryValidationError, PlacementError) as exc:
        raise vkt.UserError(str(exc)) from exc
