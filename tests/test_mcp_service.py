import base64
import hashlib
import json
import httpx
from mcp import Client
import pytest

from regnetwork_mcp.server import create_server
from regnetwork_mcp.service import ResearchService, TOOLS
from regnetwork_mcp.downloads import OfficialFileDownloader, SEVEN_ZIP_MAGIC
from regnetwork_mcp.upstream import OnlineAdapter


def row(source='TP53',target='BAX',source_id='NCBI:7157',target_id='NCBI:581'):
    return '\t'.join([source,source_id,target,target_id,'TF','Gene','core','High','1','TRRUST','0.8','123(Experimental Research)'])


def transport(request):
    if request.url.path.endswith('get_statistics'):
        series={'categories':['Node','Edge','TF-Gene'],'RegNetwork_v1':[2,1,1],'RegNetwork_core_v2':[2,1,1],'RegNetwork_v2':[2,1,1]}
        return httpx.Response(200,json={'human':series,'mouse':series})
    body=json.loads(request.content)
    if request.url.path.endswith('query'):
        records=[row(),row(source='TP53INP1',source_id='NCBI:94241'),row(source='MYC',source_id='NCBI:4609',target='TP53',target_id='NCBI:7157'),row(target='TP53',target_id='NCBI:7157')] if body['searchSymbol']=='TP53' else []
        return httpx.Response(200,json={'result':records,'totalResults':len(records)})
    return httpx.Response(200,text='\n'.join([row(),row(source='MYC',source_id='NCBI:4609',target='TP53',target_id='NCBI:7157'),row(target='TP53',target_id='NCBI:7157'),row(target='OUTSIDE',target_id='NCBI:99999')]),headers={'Content-Type':'text/plain'})


@pytest.mark.asyncio
async def test_nine_actual_sdk_tools_and_all_formats_and_resource_readback(tmp_path):
    service=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(transport),interval=0))
    async with Client(create_server(service=service)) as client:
        listing=await client.list_tools()
        assert set(t.name for t in listing.tools)==set(TOOLS)
        assert all(t.output_schema and 'status' in t.output_schema['properties'] for t in listing.tools)
        assert all(t.input_schema['additionalProperties'] is False for t in listing.tools)
        async def call(name,arguments):
            result=await client.call_tool(name,arguments)
            assert not result.is_error
            assert result.structured_content is not None
            return result.structured_content
        status=await call('get_database_status',{})
        assert status['data']['local_full_snapshot_loaded'] is False
        resolution=await call('resolve_entities',{'species':'human','dataset':'core','symbols':['TP53']})
        identifier=resolution['data']['resolutions'][0]['candidates'][0]['entity_id']
        search=await call('search_interactions',{'species':'human','dataset':'core','regulator_symbol':'TP53','target_symbol':'BAX'})
        assert len(search['data']['records'])==1
        neighbors=await call('get_neighbors',{'species':'human','dataset':'core','entity_id':identifier,'direction':'incoming'})
        assert len(neighbors['data']['records'])==2
        graph=await call('extract_subnetwork',{'species':'human','dataset':'core','symbols':['TP53','BAX','MYC','UNKNOWN']})
        assert graph['data']['excluded_outside_input']==1
        assert all(r['target']['symbol']!='OUTSIDE' for r in graph['data']['record_preview'])
        network_id=graph['data']['subnetwork_id']
        summary=await call('summarize_network',{'subnetwork_id':network_id})
        assert summary['data']['unique_endpoint_pair_count']==3
        evidence=await call('get_interaction_evidence',{'interaction_id':search['data']['records'][0]['interaction_id']})
        assert evidence['data']['references'][0]['pmid']=='123'
        assert evidence['data']['paper_full_text_fetched'] is False
        for format_name in ['tsv','json','graphml']:
            exported=await call('export_network',{'subnetwork_id':network_id,'format':format_name})
            contents=(await client.read_resource(exported['data']['resource_uri'])).contents
            assert hashlib.sha256(base64.b64decode(contents[0].blob)).hexdigest()==exported['data']['sha256']
        metadata=await client.read_resource('regnetwork://metadata')
        assert json.loads(metadata.contents[0].text)['whole_database_required'] is False


@pytest.mark.asyncio
async def test_official_download_is_a_bounded_mcp_tool_with_no_arbitrary_url(tmp_path):
    archive=SEVEN_ZIP_MAGIC+b'fixture archive'
    downloader=OfficialFileDownloader(tmp_path/'downloads',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,content=archive,headers={'Content-Type':'application/x-7z-compressed'})))
    service=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(transport),interval=0),downloader=downloader)
    async with Client(create_server(service=service)) as client:
        tools={tool.name:tool for tool in (await client.list_tools()).tools}
        assert set(tools)==set(TOOLS)
        schema=tools['download_official_file'].input_schema
        assert schema['properties']['file_category']['enum']==[
            'tf_target','mirna_target','lncrna_target','circrna_target','regulator_tf',
            'regulator_mirna','regulator_lncrna','regulator_circrna','regulator_gene','regulator_target']
        assert tools['download_official_file'].annotations.read_only_hint is False
        assert tools['download_official_file'].annotations.destructive_hint is False
        result=await client.call_tool('download_official_file',{
            'species':'human','dataset':'core','file_category':'tf_target'})
        assert not result.is_error
        data=result.structured_content
        assert data['status']=='ok'
        assert data['data']['filename']=='human_core_TF_Target.7z'
        assert data['coverage']['download_complete'] is True
        assert data['coverage']['archive_extracted'] is False
        assert data['provenance']['upstream_checksum_provided'] is False
        assert (tmp_path/'downloads'/'human_core_TF_Target.7z').read_bytes()==archive
        arbitrary=await client.call_tool('download_official_file',{
            'species':'human','dataset':'core','file_category':'tf_target','url':'https://attacker.invalid/'})
        assert arbitrary.is_error
        assert arbitrary.structured_content['error']['code']=='UNKNOWN_ARGUMENT'


@pytest.mark.asyncio
async def test_native_protocol_errors_for_invalid_parameters_and_missing_artifacts(tmp_path):
    service=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(lambda r:pytest.fail('must not request upstream')),interval=0))
    async with Client(create_server(service=service)) as client:
        await client.list_tools()
        invalid=await client.call_tool('get_neighbors',{'species':'human','dataset':'core','symbol':'TP53','limit':999})
        assert invalid.is_error
        unknown=await client.call_tool('search_interactions',{'species':'human','dataset':'core','regulator_symbol':'TP53','minimum_score_typo':0.8})
        assert unknown.is_error and unknown.structured_content['error']['code']=='UNKNOWN_ARGUMENT'
        absent=await client.call_tool('summarize_network',{'subnetwork_id':'not-found'})
        assert absent.is_error and absent.structured_content['error']['code']=='ARTIFACT_NOT_FOUND'
        unresolved=await client.call_tool('get_neighbors',{'species':'human','dataset':'core','entity_id':'unseen'})
        assert unresolved.is_error and unresolved.structured_content['error']['code']=='ENTITY_ID_NOT_RESOLVED'


@pytest.mark.asyncio
async def test_http_failure_through_mcp_is_not_empty_biological_result(tmp_path):
    service=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(lambda r:httpx.Response(500)),interval=0))
    async with Client(create_server(service=service)) as client:
        result=await client.call_tool('search_interactions',{'species':'human','dataset':'core','regulator_symbol':'TP53'})
        assert result.is_error and result.structured_content['status']=='error'
        assert result.structured_content['error']['code']=='UPSTREAM_HTTP_ERROR'
        assert result.structured_content['data']=={}


@pytest.mark.asyncio
async def test_scoped_pair_ids_and_restart_artifact_reuse(tmp_path):
    service=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(transport),interval=0))
    async with Client(create_server(service=service)) as client:
        search=await client.call_tool('search_interactions',{'species':'human','dataset':'core','regulator_symbol':'TP53','target_symbol':'BAX'})
        record=search.structured_content['data']['records'][0]
        parameters={'species':'human','dataset':'core','regulator_entity_id':record['regulator']['entity_id'],'target_entity_id':record['target']['entity_id']}
        pair=await client.call_tool('search_interactions',parameters)
        assert not pair.is_error and len(pair.structured_content['data']['records'])==1
        conflict=await client.call_tool('search_interactions',parameters|{'regulator_symbol':'MYC'})
        assert conflict.is_error and conflict.structured_content['error']['code']=='ENTITY_HINT_CONFLICT'
        for scope in [{'species':'mouse'},{'dataset':'complete'}]:
            mismatch=await client.call_tool('search_interactions',parameters|scope)
            assert mismatch.is_error and mismatch.structured_content['error']['code']=='ENTITY_ID_NOT_RESOLVED'
        graph=await client.call_tool('extract_subnetwork',{'species':'human','dataset':'core','symbols':['TP53','BAX','MYC']})
        network_id=graph.structured_content['data']['subnetwork_id']
        artifact=await client.call_tool('export_network',{'subnetwork_id':network_id,'format':'json'})
        uri=artifact.structured_content['data']['resource_uri']
    restarted=ResearchService(tmp_path,adapter=OnlineAdapter(transport=httpx.MockTransport(lambda r:pytest.fail('historical artifact does not require upstream')),interval=0))
    async with Client(create_server(service=restarted)) as client:
        summary=await client.call_tool('summarize_network',{'subnetwork_id':network_id})
        assert not summary.is_error and summary.structured_content['data']['unique_endpoint_pair_count']==3
        evidence=await client.call_tool('get_interaction_evidence',{'interaction_id':record['interaction_id']})
        assert not evidence.is_error and evidence.structured_content['data']['references'][0]['pmid']=='123'
        content=base64.b64decode((await client.read_resource(uri)).contents[0].blob)
        assert hashlib.sha256(content).hexdigest()==artifact.structured_content['data']['sha256']
