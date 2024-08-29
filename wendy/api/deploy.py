import os
import shutil
import zipfile
import tarfile
import tempfile
from io import BytesIO

import structlog
import aiodocker
from tortoise.transactions import atomic
from fastapi import APIRouter, Body, File, UploadFile

from wendy.cluster import Cluster
from wendy.constants import DeployStatus
from wendy import models, agent
from wendy.settings import DOCKER_API_DEFAULT


router = APIRouter()
log = structlog.get_logger()


@router.post(
    "",
    description="部署并启动",
)
@atomic(connection_name="default")
async def create(
    cluster: Cluster = Body(),
):
    deploy = await models.Deploy.create(
        cluster=cluster.model_dump(),
        status=DeployStatus.pending.value,
    )
    cluster = await agent.deploy(deploy.id, cluster)
    # 更新部署后的状态
    deploy.cluster = cluster.model_dump()
    deploy.status = DeployStatus.running.value
    await deploy.save()
    return deploy


@router.put(
    "/{id}",
    description="更新配置并部署",
)
async def update(
    id: int,
    cluster: Cluster = Body(),
):
    deploy = await models.Deploy.get(id=id)
    cluster = await agent.deploy(deploy.id, cluster)
    deploy.cluster = cluster.model_dump()
    deploy.status = DeployStatus.running.value
    await deploy.save()
    return deploy


@router.get(
    "",
    description="获取所有部署",
)
async def reads(
    status: DeployStatus,
):
    return await models.Deploy.filter(status=status.value).all()


@router.get(
    "/{id}",
    description="获取部署",
)
async def read(id: int):
    return await models.Deploy.get(id=id)


@router.delete(
    "/{id}",
    description="删除部署",
)
async def remove(id: int):
    deploy = await models.Deploy.get(id=id)
    cluster = Cluster.model_validate(deploy.cluster)
    await agent.delete(cluster)
    return await models.Deploy.filter(id=id).delete()


@router.get(
    "/stop/{id}",
    description="停止",
)
async def stop(id: int):
    deploy = await models.Deploy.get(id=id)
    cluster = Cluster.model_validate(deploy.cluster)
    await agent.stop(cluster)
    return await models.Deploy.filter(id=id).update(status=DeployStatus.stop.value)


@router.get(
    "/restart/{id}",
    description="重启",
)
async def restart(id: int):
    deploy = await models.Deploy.get(id=id)
    cluster = Cluster.model_validate(deploy.cluster)
    await agent.deploy(deploy.id, cluster)
    deploy.cluster = cluster.model_dump()
    deploy.status = DeployStatus.running.value
    await deploy.save()
    return "ok"


@router.post("/upload", description="上传部署")
async def upload(
    docker_api: str = Body(default=DOCKER_API_DEFAULT),
    file: UploadFile = File(),
):
    """上传文件部署.

    Args:
        file (UploadFile, optional): 文件.
    """
    filename = file.filename
    file_content = await file.read(1_073_741_824)
    # 切分上传文件后缀
    _, suffix = os.path.splitext(filename)
    if suffix == ".zip":
        temp_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(BytesIO(file_content), "r") as zip_ref:
            zip_ref.extractall(temp_dir)
    elif suffix == ".tar":
        temp_dir = tempfile.mkdtemp()
        with tarfile.open(fileobj=BytesIO(file_content), mode="r:*") as tar_ref:
            tar_ref.extractall(temp_dir)
    else:
        raise ValueError(f"Unsupported {suffix}")
    target_file = "cluster.ini"
    cluster_path = None
    for dirpath, _, filenames in os.walk(temp_dir):
        if target_file in filenames:
            # 类似  /tmp/tmpunk2y5rs/xx/xxxxxxx/Cluster_x
            cluster_path = dirpath
    # 无法定位到目录
    if cluster_path is None:
        raise ValueError(f"not found {target_file}")
    cluster = Cluster.create_from_dir(
        cluster_path,
        docker_api,
    )
    deploy = await models.Deploy.create(
        cluster=cluster.model_dump(),
        status=DeployStatus.pending.value,
    )
    # 读取当前
    parent_directory = os.path.dirname(cluster_path)
    rename_cluster_path = os.path.join(parent_directory, "Cluster_1")
    shutil.move(cluster_path, rename_cluster_path)
    async with aiodocker.Docker(docker_api) as docker:
        await agent.upload_archive(
            id=deploy.id,
            cluster_path=parent_directory,
            docker=docker,
        )
    await agent.deploy(deploy.id, cluster)
    deploy.cluster = cluster.model_dump()
    deploy.status = DeployStatus.running.value
    await deploy.save()
    return deploy
