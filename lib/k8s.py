from kubernetes import client, config
from kubernetes.client.rest import ApiException
import subprocess
import time
import sys
from lib.logger import logger
from config import app_config


def get_k8s_nodes():
    """
    Returns a list of kubernetes nodes
    """
    config.load_kube_config()
    k8s_api = client.CoreV1Api()
    logger.info("Getting k8s nodes...")
    response = k8s_api.list_node()
    logger.info("Current k8s node count is {}".format(len(response.items)))
    return response.items


def get_node_by_instance_id(k8s_nodes, instance_id):
    """
    Returns a K8S node name given an instance id. Expects the output of
    list_nodes as in input
    """
    node_name = ""
    logger.info('Searching for k8s node name by instance id...')
    for k8s_node in k8s_nodes:
        if instance_id in k8s_node.spec.provider_id:
            logger.info('InstanceId {} is node {} in kuberentes land'.format(instance_id, k8s_node.metadata.name))
            node_name = k8s_node.metadata.name
    if not node_name:
        logger.info("Could not find a k8s node name for that instance id. Exiting")
        raise Exception("Could not find a k8s node name for that instance id. Exiting")
    return node_name


def modify_k8s_autoscaler(action):
    """
    Pauses or resumes the Kubernetes autoscaler
    """
    import kubernetes.client
    config.load_kube_config()
    k8s_api = client.CoreV1Api()
    # Configure API key authorization: BearerToken
    configuration = kubernetes.client.Configuration()
    # create an instance of the API class
    k8s_api = kubernetes.client.AppsV1Api(kubernetes.client.ApiClient(configuration))
    if action == 'pause':
        logger.info('Pausing k8s autoscaler...')
        body = {'spec': {'replicas': 0}}
    elif action == 'resume':
        logger.info('Resuming k8s autoscaler...')
        body = {'spec': {'replicas': 2}}
    else:
        logger.info('Invalid k8s autoscaler option')
        sys.exit(1)
    try:
        k8s_api.patch_namespaced_deployment(
            app_config['K8S_AUTOSCALER_DEPLOYMENT'],
            app_config['K8S_AUTOSCALER_NAMESPACE'],
            body
        )
        logger.info('K8s autoscaler modified to replicas: {}'.format(body['spec']['replicas']))
    except ApiException as e:
        logger.info('Scaling of k8s autoscaler failed. Error code was {}, {}. Exiting.'.format(e.reason, e.body))
        sys.exit(1)


def delete_node(node_name):
    """
    Deletes a kubernetes node from the cluster
    """
    import kubernetes.client
    config.load_kube_config()
    k8s_api = client.CoreV1Api()
    configuration = kubernetes.client.Configuration()
    # create an instance of the API class
    k8s_api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient(configuration))
    logger.info("Deleting k8s node {}...".format(node_name))
    try:
        if not app_config['DRY_RUN']:
            k8s_api.delete_node(node_name)
        else:
            k8s_api.delete_node(node_name, dry_run="true")
        logger.info("Node deleted")
    except ApiException as e:
        logger.info("Exception when calling CoreV1Api->delete_node: {}".format(e))


def drain_node(node_name):
    """
    Executes kubectl commands to drain the node. We are not using the api
    because the draining functionality is done client side and to
    replicate the same functionality here would be too time consuming
    """
    logger.info('Draining worker node {}...'.format(node_name))
    if app_config['DRY_RUN']:
        result = subprocess.run([
            'kubectl', 'drain', node_name,
            '--ignore-daemonsets',
            '--delete-local-data',
            '--dry-run'
        ]
        )
    else:
        result = subprocess.run([
            'kubectl', 'drain', node_name,
            '--ignore-daemonsets',
            '--delete-local-data'
        ]
        )
    # If returncode is non-zero, raise a CalledProcessError.
    if result.returncode != 0:
        raise Exception("Node not drained properly. Exiting")


def k8s_nodes_ready(max_retry=app_config['GLOBAL_MAX_RETRY'], wait=app_config['GLOBAL_HEALTH_WAIT']):
    """
    Checks that all nodes in a cluster are Ready
    """
    logger.info('Checking k8s nodes health status...')
    retry_count = 1
    while retry_count < max_retry:
        # reset healthy nodes after every loop
        healthy_nodes = True
        retry_count += 1
        nodes = get_k8s_nodes()
        for node in nodes:
            conditions = node.status.conditions
            for condition in conditions:
                if condition.type == "Ready" and condition.status == "False":
                    logger.info("Node {} is not healthy - Ready: {}".format(
                        node.metadata.name,
                        condition.status)
                    )
                    healthy_nodes = False
                elif condition.type == "Ready" and condition.status == "True":
                    # condition status is a string
                    logger.info("Node {}: Ready".format(node.metadata.name))
        if healthy_nodes:
            logger.info('All k8s nodes are healthy')
            break
        logger.info('Retrying node health...')
        time.sleep(wait)
    return healthy_nodes


def k8s_nodes_count(desired_node_count, max_retry=app_config['GLOBAL_MAX_RETRY'], wait=app_config['GLOBAL_HEALTH_WAIT']):
    """
    Checks that the number of nodes in k8s cluster matches given desired_node_count
    """
    logger.info('Checking k8s expected nodes are online after asg scaled up...')
    retry_count = 1
    while retry_count < max_retry:
        nodes_online = True
        retry_count += 1
        nodes = get_k8s_nodes()
        logger.info('Current k8s node count is {}'.format(len(nodes)))
        if len(nodes) != desired_node_count:
            nodes_online = False
            logger.info('Waiting for k8s nodes to reach count {}...'.format(desired_node_count))
            time.sleep(wait)
        else:
            logger.info('Reached desired k8s node count of {}'.format(len(nodes)))
            break
    return nodes_online
