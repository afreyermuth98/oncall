import logging

from celery.utils.log import get_task_logger
from django.apps import apps
from django.core.cache import cache
from rest_framework import status

from apps.grafana_plugin.helpers import GrafanaAPIClient
from common.custom_celery_tasks import shared_dedicated_queue_retry_task

logger = get_task_logger(__name__)
logger.setLevel(logging.DEBUG)


def get_cache_key_create_contact_points_for_datasource(alert_receive_channel_id):
    CACHE_KEY_PREFIX = "create_contact_points_for_datasource"
    return f"{CACHE_KEY_PREFIX}_{alert_receive_channel_id}"


@shared_dedicated_queue_retry_task
def schedule_create_contact_points_for_datasource(alert_receive_channel_id, datasource_list):
    CACHE_LIFETIME = 600
    START_TASK_DELAY = 3
    task = create_contact_points_for_datasource.apply_async(
        args=[alert_receive_channel_id, datasource_list], countdown=START_TASK_DELAY
    )
    cache_key = get_cache_key_create_contact_points_for_datasource(alert_receive_channel_id)
    cache.set(cache_key, task.id, timeout=CACHE_LIFETIME)


@shared_dedicated_queue_retry_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=10)
def create_contact_points_for_datasource(alert_receive_channel_id, datasource_list):
    """
    Try to create contact points for other datasource.
    Restart task for datasource, for which contact point was not created.
    """
    cache_key = get_cache_key_create_contact_points_for_datasource(alert_receive_channel_id)
    cached_task_id = cache.get(cache_key)
    current_task_id = create_contact_points_for_datasource.request.id
    if cached_task_id is not None and current_task_id != cached_task_id:
        return

    AlertReceiveChannel = apps.get_model("alerts", "AlertReceiveChannel")

    alert_receive_channel = AlertReceiveChannel.objects.filter(pk=alert_receive_channel_id).first()
    if not alert_receive_channel:
        logger.debug(
            f"Cannot create contact point for integration {alert_receive_channel_id}: integration does not exist"
        )
        return

    grafana_alerting_sync_manager = alert_receive_channel.grafana_alerting_sync_manager

    client = GrafanaAPIClient(
        api_url=alert_receive_channel.organization.grafana_url,
        api_token=alert_receive_channel.organization.api_token,
    )
    # list of datasource for which contact point creation was failed
    datasources_to_create = []
    for datasource in datasource_list:
        contact_point = None
        is_grafana_datasource = not (datasource.get("id") or datasource.get("uid"))
        config, response_info = grafana_alerting_sync_manager.alerting_config_with_respect_to_grafana_version(
            is_grafana_datasource, datasource.get("id"), datasource.get("uid"), client.get_alerting_config
        )
        if config is None:
            logger.debug(
                f"Got config None for is_grafana_datasource {is_grafana_datasource} "
                f"for integration {alert_receive_channel_id}; response: {response_info}"
            )
            if response_info.get("status_code") == status.HTTP_404_NOT_FOUND:
                grafana_alerting_sync_manager.alerting_config_with_respect_to_grafana_version(
                    is_grafana_datasource,
                    datasource.get("id"),
                    datasource.get("uid"),
                    client.get_alertmanager_status_with_config,
                )
                contact_point = grafana_alerting_sync_manager.create_contact_point(datasource)
            elif response_info.get("status_code") == status.HTTP_400_BAD_REQUEST:
                logger.warning(
                    f"Failed to create contact point for integration {alert_receive_channel_id}, "
                    f"datasource info: {datasource}; response: {response_info}"
                )
                continue
        else:
            contact_point = grafana_alerting_sync_manager.create_contact_point(datasource)
        if contact_point is None:
            logger.warning(
                f"Failed to create contact point for integration {alert_receive_channel_id} due to getting wrong "
                f"config, datasource info: {datasource}; response: {response_info}. Retrying"
            )
            # Failed to create contact point due to getting wrong alerting config.
            # Add datasource to list and retry to create contact point for it again
            datasources_to_create.append(datasource)

    # if some contact points were not created, restart task for them
    if datasources_to_create:
        schedule_create_contact_points_for_datasource(alert_receive_channel_id, datasources_to_create)
    else:
        alert_receive_channel.is_finished_alerting_setup = True
        alert_receive_channel.save(update_fields=["is_finished_alerting_setup"])
