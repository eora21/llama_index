import base64
import urllib.parse
import uuid
from httpx import Request, HTTPStatusError
from tenacity import (
    retry,
    wait_exponential_jitter,
    stop_after_attempt,
    retry_if_exception,
)
from typing import Any, Optional, Tuple, List, Union, Dict, Callable

from llama_index.core.async_utils import run_jobs
from llama_index.core.schema import NodeWithScore, ImageNode
from llama_cloud import (
    AutoTransformConfig,
    PageFigureNodeWithScore,
    PageScreenshotNodeWithScore,
    Pipeline,
    PipelineCreateTransformConfig,
    PipelineType,
    Project,
    Retriever,
)
from llama_cloud.core import remove_none_from_dict
from llama_cloud.client import LlamaCloud, AsyncLlamaCloud
from llama_cloud.core.api_error import ApiError


def is_retryable_http_error(exception):
    # Retry for ApiError with 5xx status codes
    if isinstance(exception, ApiError):
        return 500 <= exception.status_code < 600
    # Also retry for HTTPError with 5xx status codes
    elif isinstance(exception, HTTPStatusError):
        return 500 <= exception.response.status_code < 600
    return False


def retry_on_failure(func: Callable) -> Callable:
    """Decorator to apply tenacity retry with exponential backoff and jitter for 5xx errors."""
    return retry(
        wait=wait_exponential_jitter(exp_base=2, max=10),
        stop=stop_after_attempt(5),
        retry=retry_if_exception(is_retryable_http_error),
        reraise=True,
    )(func)


def default_transform_config() -> PipelineCreateTransformConfig:
    return AutoTransformConfig()


def resolve_retriever(
    client: LlamaCloud,
    project: Project,
    retriever_name: Optional[str] = None,
    retriever_id: Optional[str] = None,
    persisted: bool = True,
) -> Optional[Retriever]:
    if not persisted:
        return Retriever(
            id=str(uuid.uuid4()),
            project_id=project.id,
            name=retriever_name,
            pipelines=[],
        )
    if retriever_id:
        return client.retrievers.get_retriever(
            retriever_id=retriever_id, project_id=project.id
        )
    elif retriever_name:
        retrievers = client.retrievers.list_retrievers(
            project_id=project.id, name=retriever_name
        )
        return next(
            (retriever for retriever in retrievers if retriever.name == retriever_name),
            None,
        )
    else:
        return None


def resolve_project(
    client: LlamaCloud,
    project_name: Optional[str],
    project_id: Optional[str],
    organization_id: Optional[str],
) -> Project:
    if project_id is not None:
        return client.projects.get_project(project_id=project_id)
    else:
        projects = client.projects.list_projects(
            project_name=project_name, organization_id=organization_id
        )
        if len(projects) == 0:
            raise ValueError(f"No project found with name {project_name}")
        elif len(projects) > 1:
            raise ValueError(
                f"Multiple projects found with name {project_name}. Please specify organization_id."
            )
        return projects[0]


def resolve_pipeline(
    client: LlamaCloud,
    pipeline_id: Optional[str],
    project: Optional[Project],
    pipeline_name: Optional[str],
) -> Pipeline:
    if pipeline_id is not None:
        return client.pipelines.get_pipeline(pipeline_id=pipeline_id)
    else:
        pipelines = client.pipelines.search_pipelines(
            project_id=project.id,
            pipeline_name=pipeline_name,
            pipeline_type=PipelineType.MANAGED.value,
        )
        if len(pipelines) == 0:
            raise ValueError(
                f"Unknown index name {pipeline_name}. Please confirm an index with this name exists."
            )
        elif len(pipelines) > 1:
            raise ValueError(
                f"Multiple pipelines found with name {pipeline_name} in project {project.name}"
            )
        return pipelines[0]


def resolve_project_and_pipeline(
    client: LlamaCloud,
    pipeline_name: Optional[str],
    pipeline_id: Optional[str],
    project_name: Optional[str],
    project_id: Optional[str],
    organization_id: Optional[str],
) -> Tuple[Project, Pipeline]:
    # resolve pipeline by ID
    if pipeline_id is not None:
        pipeline = resolve_pipeline(
            client, pipeline_id=pipeline_id, project=None, pipeline_name=None
        )
        project_id = pipeline.project_id

    # resolve project
    project = resolve_project(client, project_name, project_id, organization_id)

    # resolve pipeline by name
    if pipeline_id is None:
        pipeline = resolve_pipeline(
            client, pipeline_id=None, project=project, pipeline_name=pipeline_name
        )

    return project, pipeline


def _build_get_page_screenshot_request(
    client: Union[LlamaCloud, AsyncLlamaCloud],
    file_id: str,
    page_index: int,
    project_id: str,
) -> Request:
    return client._client_wrapper.httpx_client.build_request(
        "GET",
        urllib.parse.urljoin(
            f"{client._client_wrapper.get_base_url()}/",
            f"api/v1/files/{file_id}/page_screenshots/{page_index}",
        ),
        params=remove_none_from_dict({"project_id": project_id}),
        headers=client._client_wrapper.get_headers(),
        timeout=60,
    )


def _build_get_page_figure_request(
    client: Union[LlamaCloud, AsyncLlamaCloud],
    file_id: str,
    page_index: int,
    figure_name: str,
    project_id: str,
) -> Request:
    return client._client_wrapper.httpx_client.build_request(
        "GET",
        urllib.parse.urljoin(
            f"{client._client_wrapper.get_base_url()}/",
            f"api/v1/files/{file_id}/page-figures/{page_index}/{figure_name}",
        ),
        params=remove_none_from_dict({"project_id": project_id}),
        headers=client._client_wrapper.get_headers(),
        timeout=60,
    )


@retry_on_failure
def get_page_screenshot(
    client: LlamaCloud, file_id: str, page_index: int, project_id: str
) -> str:
    """Get the page screenshot."""
    # TODO: this currently uses requests, should be replaced with the client
    request = _build_get_page_screenshot_request(
        client, file_id, page_index, project_id
    )
    _response = client._client_wrapper.httpx_client.send(request)
    if 200 <= _response.status_code < 300:
        return _response.content
    else:
        raise ApiError(status_code=_response.status_code, body=_response.text)


@retry_on_failure
def get_page_figure(
    client: LlamaCloud, file_id: str, page_index: int, figure_name: str, project_id: str
) -> str:
    request = _build_get_page_figure_request(
        client, file_id, page_index, figure_name, project_id
    )
    _response = client._client_wrapper.httpx_client.send(request)
    if 200 <= _response.status_code < 300:
        return _response.content
    else:
        raise ApiError(status_code=_response.status_code, body=_response.text)


@retry_on_failure
async def aget_page_screenshot(
    client: AsyncLlamaCloud, file_id: str, page_index: int, project_id: str
) -> str:
    """Get the page screenshot (async)."""
    request = _build_get_page_screenshot_request(
        client, file_id, page_index, project_id
    )
    _response = await client._client_wrapper.httpx_client.send(request)
    if 200 <= _response.status_code < 300:
        return _response.content
    else:
        raise ApiError(status_code=_response.status_code, body=_response.text)


@retry_on_failure
async def aget_page_figure(
    client: AsyncLlamaCloud,
    file_id: str,
    page_index: int,
    figure_name: str,
    project_id: str,
) -> str:
    request = _build_get_page_figure_request(
        client, file_id, page_index, figure_name, project_id
    )
    _response = await client._client_wrapper.httpx_client.send(request)
    if 200 <= _response.status_code < 300:
        return _response.content
    else:
        raise ApiError(status_code=_response.status_code, body=_response.text)


def page_screenshot_nodes_to_node_with_score(
    client: LlamaCloud,
    raw_image_nodes: Optional[List[PageScreenshotNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    if not raw_image_nodes:
        return []

    image_nodes = []
    for raw_image_node in raw_image_nodes:
        image_bytes = get_page_screenshot(
            client=client,
            file_id=raw_image_node.node.file_id,
            page_index=raw_image_node.node.page_index,
            project_id=project_id,
        )
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        image_node_metadata: Dict[str, Any] = {
            **(raw_image_node.node.metadata or {}),
            "file_id": raw_image_node.node.file_id,
            "page_index": raw_image_node.node.page_index,
        }
        image_node_with_score = NodeWithScore(
            node=ImageNode(image=image_base64, metadata=image_node_metadata),
            score=raw_image_node.score,
        )
        image_nodes.append(image_node_with_score)

    return image_nodes


def image_nodes_to_node_with_score(
    client: LlamaCloud,
    raw_image_nodes: Optional[List[PageScreenshotNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    """
    Legacy method to alias page_screenshot_nodes_to_node_with_score.
    """
    if not raw_image_nodes:
        return []

    return page_screenshot_nodes_to_node_with_score(
        client=client, raw_image_nodes=raw_image_nodes, project_id=project_id
    )


def page_figure_nodes_to_node_with_score(
    client: LlamaCloud,
    raw_figure_nodes: Optional[List[PageFigureNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    if not raw_figure_nodes:
        return []

    figure_nodes = []
    for raw_figure_node in raw_figure_nodes:
        figure_bytes = get_page_figure(
            client=client,
            file_id=raw_figure_node.node.file_id,
            page_index=raw_figure_node.node.page_index,
            figure_name=raw_figure_node.node.figure_name,
            project_id=project_id,
        )
        figure_base64 = base64.b64encode(figure_bytes).decode("utf-8")
        figure_node_metadata: Dict[str, Any] = {
            **(raw_figure_node.node.metadata or {}),
            "file_id": raw_figure_node.node.file_id,
            "page_index": raw_figure_node.node.page_index,
            "figure_name": raw_figure_node.node.figure_name,
        }
        figure_node_with_score = NodeWithScore(
            node=ImageNode(image=figure_base64, metadata=figure_node_metadata),
            score=raw_figure_node.score,
        )
        figure_nodes.append(figure_node_with_score)
    return figure_nodes


async def apage_screenshot_nodes_to_node_with_score(
    client: AsyncLlamaCloud,
    raw_image_nodes: Optional[List[PageScreenshotNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    if not raw_image_nodes:
        return []

    image_nodes = []
    tasks = [
        aget_page_screenshot(
            client=client,
            file_id=raw_image_node.node.file_id,
            page_index=raw_image_node.node.page_index,
            project_id=project_id,
        )
        for raw_image_node in raw_image_nodes
    ]

    image_bytes_list = await run_jobs(tasks)
    for image_bytes, raw_image_node in zip(image_bytes_list, raw_image_nodes):
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        image_node_metadata: Dict[str, Any] = {
            **(raw_image_node.node.metadata or {}),
            "file_id": raw_image_node.node.file_id,
            "page_index": raw_image_node.node.page_index,
        }
        image_node_with_score = NodeWithScore(
            node=ImageNode(image=image_base64, metadata=image_node_metadata),
            score=raw_image_node.score,
        )
        image_nodes.append(image_node_with_score)
    return image_nodes


async def aimage_nodes_to_node_with_score(
    client: AsyncLlamaCloud,
    raw_image_nodes: Optional[List[PageScreenshotNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    """
    Legacy method to alias apage_screenshot_nodes_to_node_with_score.
    """
    if not raw_image_nodes:
        return []

    return await apage_screenshot_nodes_to_node_with_score(
        client=client, raw_image_nodes=raw_image_nodes, project_id=project_id
    )


async def apage_figure_nodes_to_node_with_score(
    client: AsyncLlamaCloud,
    raw_figure_nodes: Optional[List[PageFigureNodeWithScore]],
    project_id: str,
) -> List[NodeWithScore]:
    if not raw_figure_nodes:
        return []

    figure_nodes = []
    tasks = [
        aget_page_figure(
            client=client,
            file_id=raw_figure_node.node.file_id,
            page_index=raw_figure_node.node.page_index,
            figure_name=raw_figure_node.node.figure_name,
            project_id=project_id,
        )
        for raw_figure_node in raw_figure_nodes
    ]

    figure_bytes_list = await run_jobs(tasks)
    for figure_bytes, raw_figure_node in zip(figure_bytes_list, raw_figure_nodes):
        figure_base64 = base64.b64encode(figure_bytes).decode("utf-8")
        figure_node_metadata: Dict[str, Any] = {
            **(raw_figure_node.node.metadata or {}),
            "file_id": raw_figure_node.node.file_id,
            "page_index": raw_figure_node.node.page_index,
            "figure_name": raw_figure_node.node.figure_name,
        }
        figure_node_with_score = NodeWithScore(
            node=ImageNode(image=figure_base64, metadata=figure_node_metadata),
            score=raw_figure_node.score,
        )
        figure_nodes.append(figure_node_with_score)

    return figure_nodes
