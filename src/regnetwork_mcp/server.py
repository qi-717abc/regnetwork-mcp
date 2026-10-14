"""Eight structured MCP tools and controlled research resources."""
from contextlib import asynccontextmanager
import json
import inspect
from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .paths import resolve_data_root
from .service import ApplicationError, Envelope, ResearchService, TOOLS

Species=Literal['human','mouse']
Dataset=Literal['core','complete']
Limit=Annotated[int,Field(ge=1,le=500,strict=True)]
Offset=Annotated[int,Field(ge=0,strict=True)]
Symbols=Annotated[list[Annotated[str,Field(min_length=1,max_length=200)]],Field(min_length=1,max_length=50)]


def wire(value: Envelope):
    # SDK v2 supports a raw CallToolResult while validating against the declared Envelope output schema.
    payload=value.model_dump(mode='json')
    text=json.dumps({'status':value.status,'data_keys':list(value.data),'warnings':value.warnings,'error':value.error},ensure_ascii=False)
    return CallToolResult(content=[TextContent(type='text',text=text)],structured_content=payload,is_error=value.status in {'error','unavailable'})


def create_server(root: Path | None = None, *, service: ResearchService | None = None):
    application=service or ResearchService(resolve_data_root(root))
    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {'application':application}
        finally:
            await application.close()
    server=MCPServer('RegNetwork Research',version=__version__,lifespan=lifespan,
        instructions='线上优先、源数据库只读。明确物种与集合；保留歧义、部分分页、证据缺失和来源时间。计算结果不是生物学或因果验证。',log_level='WARNING')
    readonly=ToolAnnotations(read_only_hint=True,open_world_hint=True)
    cached=ToolAnnotations(read_only_hint=True,open_world_hint=False)

    @server.tool(annotations=readonly)
    async def get_database_status(refresh: bool=False) -> Envelope:
        """检查线上统计接口、缓存、源统计一致性和当前工具能力。"""
        return wire(await application.guard('get_database_status',{'refresh':refresh},lambda:application.database_status(refresh)))

    @server.tool(annotations=readonly)
    async def resolve_entities(species: Species,dataset: Dataset,symbols: Symbols,refresh: bool=False) -> Envelope:
        """按完整名称查询来源实体候选；保留歧义和不完整结果，返回的 ID 可用于后续查询。"""
        return wire(await application.guard('resolve_entities',{'species':species,'dataset':dataset,'symbols':symbols},lambda:application.resolve(species,dataset,symbols,refresh)))

    @server.tool(annotations=readonly)
    async def search_interactions(species: Species,dataset: Dataset,regulator_symbol: str|None=None,target_symbol: str|None=None,
        regulator_entity_id: str|None=None,target_entity_id: str|None=None,
        regulator_type: Literal['all','TF','miRNA','lncRNA','circRNA']='all',
        target_type: Literal['all','TF','miRNA','lncRNA','circRNA','Gene']='all',
        confidence: Literal['all','high','medium','low']='all',
        min_score: Annotated[float|None,Field(ge=0,le=1)]=None,limit: Limit=30,offset: Offset=0,refresh: bool=False) -> Envelope:
        """精确查询起点或终点；至少给出一端名称/已解析 ID。完整性仅针对当前名称候选查询，别名不保证穷尽。"""
        params={'species':species,'dataset':dataset,'regulator_symbol':regulator_symbol,'target_symbol':target_symbol,
                'regulator_entity_id':regulator_entity_id,'target_entity_id':target_entity_id,'regulator_type':regulator_type,
                'target_type':target_type,'confidence':confidence,'min_score':min_score,'limit':limit,'offset':offset,'refresh':refresh}
        return wire(await application.guard('search_interactions',params,lambda:application.search(**params)))

    @server.tool(annotations=readonly)
    async def get_neighbors(species: Species,dataset: Dataset,symbol: str|None=None,entity_id: str|None=None,
        direction: Literal['incoming','outgoing','both']='both',limit: Limit=100,offset: Offset=0,refresh: bool=False) -> Envelope:
        """查询一跳上游/下游，保留方向、来源和截断；陌生纯 ID 需先用名称解析。"""
        params={'species':species,'dataset':dataset,'symbol':symbol,'entity_id':entity_id,'direction':direction,
                'limit':limit,'offset':offset,'refresh':refresh}
        return wire(await application.guard('get_neighbors',params,lambda:application.neighbors(**params)))

    @server.tool(annotations=readonly)
    async def extract_subnetwork(species: Species,dataset: Dataset,symbols: Symbols,
        max_edges: Annotated[int,Field(ge=1,le=5000,strict=True)]=5000,refresh: bool=False) -> Envelope:
        """提取输入名称集合内部的有向网络；返回 subnetwork_id、输入状态和最多30条预览，完整边用于导出。"""
        return wire(await application.guard('extract_subnetwork',{'species':species,'dataset':dataset,'symbols':symbols},lambda:application.subnetwork(species,dataset,symbols,max_edges=max_edges,refresh=refresh)))

    @server.tool(annotations=cached)
    async def summarize_network(subnetwork_id: str,include_self_loops: bool=True) -> Envelope:
        """计算已提取子网络的度数、角色组成、自环、连通性；结构地位不等于疾病驱动性。"""
        return wire(await application.guard('summarize_network',{'subnetwork_id':subnetwork_id},lambda:application.summarize(subnetwork_id,include_self_loops)))

    @server.tool(annotations=cached)
    async def get_interaction_evidence(interaction_id: str,max_references: Annotated[int,Field(ge=1,le=100,strict=True)]=20) -> Envelope:
        """读取此前查询/映射取得的来源注释和 PMID；不抓取论文全文，不把缺失值变成证据。"""
        return wire(await application.guard('get_interaction_evidence',{'interaction_id':interaction_id},lambda:application.evidence(interaction_id,max_references)))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False,destructive_hint=False,idempotent_hint=True,open_world_hint=False))
    async def export_network(subnetwork_id: str,format: Literal['tsv','json','graphml']) -> Envelope:
        """在专用目录创建可复算网络文件及校验清单，返回可读取的 MCP resource URI。"""
        return wire(await application.guard('export_network',{'subnetwork_id':subnetwork_id,'format':format},lambda:application.export(subnetwork_id,format)))

    @server.resource('regnetwork://metadata',mime_type='application/json')
    def metadata() -> str:
        return json.dumps({'version':__version__,'tools':TOOLS,'transport':'stdio','source':'RegNetwork 2025 site label',
                           'source_database_read_only':True,'whole_database_required':False,'cache_ttl_seconds':86400},ensure_ascii=False)

    @server.resource('regnetwork://schema',mime_type='application/json')
    def schema() -> str:
        return json.dumps(Envelope.model_json_schema(),ensure_ascii=False)

    @server.resource('regnetwork://limitations',mime_type='text/plain')
    def limitations() -> str:
        return ('上游内部读取接口不具有已确认的稳定公开契约。分页完整性针对当前名称候选，别名不保证穷尽；'
                '实体身份由来源声明，未作独立权威核实。缺失边不代表无生物学关系；score不是效应强度；'
                '不含激活/抑制及组织疾病条件。PCA-CMI、富集和本地整库均未启用。'
                'offset分页间若取得数据不同，应核对provenance；已有artifact保留原结果，不表示新鲜查询。')

    @server.resource('regnetwork://exports/{artifact_id}',mime_type='application/octet-stream')
    def export_resource(artifact_id: str) -> bytes:
        return application.read_export(artifact_id)

    allowed={fn.__name__:set(inspect.signature(fn).parameters) for fn in [get_database_status,resolve_entities,
        search_interactions,get_neighbors,extract_subnetwork,summarize_network,get_interaction_evidence,export_network]}
    async def strict_arguments(context,call_next):
        if context.method=='tools/call' and isinstance(context.params,dict):
            name=context.params.get('name')
            arguments=context.params.get('arguments',{})
            if name in allowed and isinstance(arguments,dict):
                unknown=set(arguments)-allowed[name]
                if unknown:
                    return wire(application.error(ApplicationError('UNKNOWN_ARGUMENT','unknown parameters: '+', '.join(sorted(unknown))),{'operation':name}))
        result=await call_next(context)
        if context.method=='tools/list':
            if isinstance(result,dict):
                for tool in result.get('tools',[]):
                    schema=tool.get('inputSchema',tool.get('input_schema'))
                    if isinstance(schema,dict):schema['additionalProperties']=False
            else:
                for tool in getattr(result,'tools',[]):
                    tool.input_schema['additionalProperties']=False
        return result
    server.middleware.append(strict_arguments)
    return server
