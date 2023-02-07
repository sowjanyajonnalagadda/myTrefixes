from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient
from pydantic import parse_obj_as
from db.repositories.resources_history import ResourceHistoryRepository
from service_bus.substitutions import substitute_properties
from models.domain.resource_template import PipelineStep
from models.domain.operation import OperationStep
from models.domain.resource import Resource, ResourceType
from db.repositories.resource_templates import ResourceTemplateRepository
from models.domain.authentication import User
from models.schemas.resource import ResourcePatch
from db.repositories.resources import ResourceRepository
from core import config, credentials
import logging
from azure.cosmos.exceptions import CosmosAccessConditionFailedError


async def _send_message(message: ServiceBusMessage, queue: str):
    """
    Sends the given message to the given queue in the Service Bus.

    :param message: The message to send.
    :type message: ServiceBusMessage
    :param queue: The Service Bus queue to send the message to.
    :type queue: str
    """
    async with credentials.get_credential_async() as credential:
        service_bus_client = ServiceBusClient(config.SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE, credential)

        async with service_bus_client:
            sender = service_bus_client.get_queue_sender(queue_name=queue)

            async with sender:
                await sender.send_messages(message)


async def send_deployment_message(content, correlation_id, session_id, action):
    resource_request_message = ServiceBusMessage(body=content, correlation_id=correlation_id, session_id=session_id)
    logging.info(f"Sending resource request message with correlation ID {resource_request_message.correlation_id}, action: {action}")
    await _send_message(resource_request_message, config.SERVICE_BUS_RESOURCE_REQUEST_QUEUE)


async def update_resource_for_step(operation_step: OperationStep, resource_repo: ResourceRepository, resource_template_repo: ResourceTemplateRepository, resource_history_repo: ResourceHistoryRepository, primary_resource: Resource, resource_to_update_id: str, primary_action: str, user: User) -> Resource:
    current_resource = await resource_repo.get_resource_by_id(operation_step.resourceId)
    # if this is main, just leave it alone and return it
    if operation_step.stepId == "main":
        return current_resource

    # get the template for the primary resource, to get all the step details for substitutions
    parent_resource = await resource_repo.get_resource_by_id(operation_step.parentResourceId)
    parent_service_name = ""
    if parent_resource.resourceType == ResourceType.UserResource:
        parent_workspace_service = await resource_repo.get_resource_by_id(parent_resource.parentWorkspaceServiceId)
        parent_service_name = parent_workspace_service.templateName
    parent_template = await resource_template_repo.get_template_by_name_and_version(parent_resource.templateName, parent_resource.templateVersion, parent_resource.resourceType, parent_service_name)

    # get the template step
    template_step = None
    for step in parent_template.pipeline.dict()[primary_action]:
        if step["stepId"] == operation_step.stepId:
            template_step = parse_obj_as(PipelineStep, step)
            break

    if template_step is None:
        raise Exception(f"Cannot find step with id of {operation_step.stepId} in template {current_resource.templateName} for action {primary_action}")

    if template_step.resourceAction == "upgrade":
        resource_to_send = await try_upgrade_with_retries(
            num_retries=3,
            attempt_count=0,
            resource_repo=resource_repo,
            resource_template_repo=resource_template_repo,
            resource_history_repo=resource_history_repo,
            user=user,
            resource_to_update_id=resource_to_update_id,
            template_step=template_step,
            primary_resource=primary_resource
        )

        return resource_to_send

    else:
        raise Exception("Only upgrade is currently supported for pipeline steps")


async def try_upgrade_with_retries(num_retries: int, attempt_count: int, resource_repo: ResourceRepository, resource_template_repo: ResourceTemplateRepository, resource_history_repo: ResourceHistoryRepository, user: User, resource_to_update_id: str, template_step: PipelineStep, primary_resource: Resource) -> Resource:
    try:
        return await try_upgrade(
            resource_repo=resource_repo,
            resource_template_repo=resource_template_repo,
            resource_history_repo=resource_history_repo,
            user=user,
            resource_to_update_id=resource_to_update_id,
            template_step=template_step,
            primary_resource=primary_resource
        )
    except CosmosAccessConditionFailedError as e:
        logging.warning(f"Etag mismatch for {resource_to_update_id}. Retrying.")
        if attempt_count < num_retries:
            await try_upgrade_with_retries(
                num_retries=num_retries,
                attempt_count=(attempt_count + 1),
                resource_repo=resource_repo,
                resource_template_repo=resource_template_repo,
                resource_history_repo=resource_history_repo,
                user=user,
                resource_to_update_id=resource_to_update_id,
                template_step=template_step,
                primary_resource=primary_resource
            )
        else:
            raise e


async def try_upgrade(resource_repo: ResourceRepository, resource_template_repo: ResourceTemplateRepository, resource_history_repo: ResourceHistoryRepository, user: User, resource_to_update_id: str, template_step: PipelineStep, primary_resource: Resource) -> Resource:
    resource_to_update = await resource_repo.get_resource_by_id(resource_to_update_id)

    # substitute values into new property bag for update
    properties = substitute_properties(template_step, primary_resource, resource_to_update)

    # get the template for the resource to upgrade
    parent_service_name = ""
    if resource_to_update.resourceType == ResourceType.UserResource:
        parent_service_name = resource_to_update["parentWorkspaceServiceId"]

    resource_template_to_send = await resource_template_repo.get_template_by_name_and_version(resource_to_update.templateName, resource_to_update.templateVersion, resource_to_update.resourceType, parent_service_name)

    # create the patch
    patch = ResourcePatch(
        properties=properties
    )

    # validate and submit the patch
    resource_to_send, _ = await resource_repo.patch_resource(
        resource=resource_to_update,
        resource_patch=patch,
        resource_template=resource_template_to_send,
        etag=resource_to_update.etag,
        resource_template_repo=resource_template_repo,
        resource_history_repo=resource_history_repo,
        user=user)

    return resource_to_send
