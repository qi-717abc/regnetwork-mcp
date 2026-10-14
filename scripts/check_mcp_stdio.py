"""Actual subprocess/stdio and live-source acceptance; no scientific validity claim."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from regnetwork_mcp.service import TOOLS


async def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--command',default=sys.executable,help='stdio server launcher; defaults to this Python interpreter')
    parser.add_argument('--server-arg',action='append',dest='server_args',help='server argument; repeat for each argument')
    parser.add_argument('--cwd',type=Path,default=root,help='working directory for the spawned server')
    parser.add_argument('--data-root',type=Path,default=root,help='REGNETWORK_MCP_ROOT for server cache and exports')
    parser.add_argument('--report',type=Path,default=root/'.runtime/verification/s07-stdio-live.json',help='where to save the protocol report')
    args=parser.parse_args()
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'kind':'actual_stdio_live_source',
            'command':args.command,'server_args':args.server_args or ['-m','regnetwork_mcp'],
            'server_cwd':str(args.cwd.resolve()),'data_root':str(args.data_root.resolve()),'calls':[]}
    env={'REGNETWORK_MCP_ROOT':str(args.data_root.resolve())}
    for name in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY']:
        if name in os.environ:env[name]=os.environ[name]
    for name in ['UV_CACHE_DIR','UV_TOOL_DIR','UV_LINK_MODE']:
        if name in os.environ:env[name]=os.environ[name]
    try:
        async with Client(StdioServerParameters(command=args.command,args=args.server_args or ['-m','regnetwork_mcp'],cwd=args.cwd,env=env),read_timeout_seconds=180) as client:
            listing=await client.list_tools()
            assert set(t.name for t in listing.tools)==set(TOOLS)
            assert all(t.input_schema['additionalProperties'] is False and t.output_schema for t in listing.tools)
            report['tools']=listing.model_dump(mode='json',by_alias=True)
            async def call(name,arguments):
                result=await client.call_tool(name,arguments)
                report['calls'].append({'tool':name,'arguments':arguments,'result':result.model_dump(mode='json',by_alias=True)})
                assert not result.is_error,(name,result.structured_content)
                assert result.structured_content is not None
                print(json.dumps({'tool':name,'status':result.structured_content['status']}),flush=True)
                return result.structured_content
            await call('get_database_status',{'refresh':True})
            resolved=await call('resolve_entities',{'species':'human','dataset':'core','symbols':['TP53'],'refresh':True})
            candidate=resolved['data']['resolutions'][0]['candidates'][0]['entity_id']
            search=await call('search_interactions',{'species':'human','dataset':'core','regulator_entity_id':candidate,'target_symbol':'BAX','limit':5})
            assert search['data']['records']
            await call('get_neighbors',{'species':'human','dataset':'core','entity_id':candidate,'direction':'incoming','limit':5})
            graph=await call('extract_subnetwork',{'species':'human','dataset':'core','symbols':['TP53','BAX','CDKN1A','MDM2','MYC','REGNETWORK_MCP_NO_SUCH_SYMBOL_20261013'],'refresh':True})
            network_id=graph['data']['subnetwork_id']
            summary=await call('summarize_network',{'subnetwork_id':network_id})
            await call('get_interaction_evidence',{'interaction_id':search['data']['records'][0]['interaction_id']})
            report['export_readback']=[]
            for format_name in ['tsv','json','graphml']:
                artifact=await call('export_network',{'subnetwork_id':network_id,'format':format_name})
                resource=await client.read_resource(artifact['data']['resource_uri'])
                content=base64.b64decode(resource.contents[0].blob)
                assert hashlib.sha256(content).hexdigest()==artifact['data']['sha256']
                report['export_readback'].append({'format':format_name,'sha256':artifact['data']['sha256'],'bytes':len(content),'passed':True})
            report['metadata']=(await client.read_resource('regnetwork://metadata')).model_dump(mode='json',by_alias=True)
            bad=await client.call_tool('get_database_status',{'unexpected_parameter':True})
            assert bad.is_error and bad.structured_content['error']['code']=='UNKNOWN_ARGUMENT'
            report['unknown_argument_rejected']=True
            report['passed']=True
            print(json.dumps({'passed':True,'nodes':summary['data']['node_count'],'edges':summary['data']['unique_endpoint_pair_count']}),flush=True)
    finally:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    asyncio.run(main())
