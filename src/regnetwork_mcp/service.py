"""Application orchestration shared by the MCP tools and verification clients."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from .analysis import _node, extract_induced, resolve_scan, select_interactions, summarize_subnetwork
from .cache import ResultCache
from .exports import export_network
from .records import parse_interaction
from .upstream import OnlineAdapter, ReadObservation, UpstreamError, UpstreamFormatError

TOOLS = ['get_database_status','resolve_entities','search_interactions','get_neighbors',
         'extract_subnetwork','summarize_network','get_interaction_evidence','export_network']


class Envelope(BaseModel):
    status: Literal['ok','empty','partial','error','unavailable']
    data: dict[str, Any]
    provenance: dict[str, Any]
    coverage: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class ApplicationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code=code


def valid_symbol(symbol: str) -> str:
    symbol=symbol.strip()
    if not symbol or len(symbol)>200 or ',' in symbol or any(ord(c)<32 for c in symbol):
        raise ApplicationError('INVALID_SYMBOL','provide one nonempty symbol, at most 200 characters; use a list for multiple symbols')
    return symbol


class ResearchService:
    def __init__(self, root: Path, *, adapter: OnlineAdapter | None = None):
        self.root=root.resolve()
        self.cache=ResultCache(self.root/'.runtime/results.sqlite')
        self.adapter=adapter or OnlineAdapter(cache=self.cache)
        self.exports=self.root/'exports'

    async def close(self):
        await self.adapter.client.aclose()

    def envelope(self, data: dict[str,Any], parameters: dict[str,Any], observations=(), *, status='ok',warnings=(),coverage=None) -> Envelope:
        return Envelope(status=status,data=data,
            provenance={'source_release':'RegNetwork 2025 (site label)','provider':'online_adapter',
                        'atomic_source_snapshot':False,'query_parameters':parameters,
                        'observations':[asdict(o) for o in observations]},
            coverage=coverage or {'biological_validation':'not_performed'},warnings=list(warnings))

    def error(self, exc: Exception, parameters: dict[str,Any]) -> Envelope:
        code=getattr(exc,'code','UPSTREAM_FORMAT_CHANGED' if isinstance(exc,UpstreamFormatError) else 'INVALID_ARGUMENT')
        value=self.envelope({},parameters,status='error')
        value.error={'code':code,'message':str(exc)}
        return value

    async def guard(self, operation: str, parameters: dict[str,Any], work) -> Envelope:
        try:
            return await work()
        except (UpstreamError,UpstreamFormatError,ApplicationError,ValueError,FileNotFoundError,FileExistsError) as exc:
            return self.error(exc,{'operation':operation}|parameters)

    def _remember_records(self, records, species, dataset, observations):
        for record in records:
            self.cache.put(ResultCache.key('interaction',record.interaction_id),
                           {'raw':record.raw_record,'species':species,'dataset':dataset,'observations':[asdict(o) for o in observations]},namespace='interaction')
            for entity in [record.regulator,record.target]:
                self._remember_entity(_node(entity),species,dataset)

    def _remember_entity(self,candidate,species,dataset):
        key=ResultCache.key('entity',[species,dataset,candidate['entity_id']])
        previous=self.cache.get(key,allow_expired=True)
        value=dict(candidate)
        if previous is not None:
            value['symbols']=sorted(set(value['symbols'])|set(previous.payload['symbols']))
            value['source_types']=sorted(set(value['source_types'])|set(previous.payload['source_types']))
        self.cache.put(key,value,namespace='entity')

    def _remember_resolution(self, resolution, species,dataset,observations=()):
        value=resolution|{'source_observations':[asdict(o) for o in observations]}
        self.cache.put(ResultCache.key('resolution',[species,dataset,resolution['symbol']]),value,namespace='resolution')
        for candidate in resolution['candidates']:
            self._remember_entity(candidate,species,dataset)

    def _selector(self,species,dataset,symbol,entity_id):
        self.adapter._scope(species,dataset)
        if entity_id:
            hit=self.cache.get(ResultCache.key('entity',[species,dataset,entity_id]),allow_expired=True)
            if hit is None:
                raise ApplicationError('ENTITY_ID_NOT_RESOLVED','resolve this ID with a symbol in the selected dataset first')
            candidate=hit.payload
            if symbol and symbol not in candidate['symbols']:
                raise ApplicationError('ENTITY_HINT_CONFLICT','symbol and previously resolved source identity differ')
            symbol=symbol or candidate['symbol']
        if symbol is None:
            raise ApplicationError('SYMBOL_REQUIRED','a symbol or previously resolved entity ID is required')
        return valid_symbol(symbol)

    async def database_status(self,refresh=False):
        data,observation=await self.adapter.statistics(refresh=refresh)
        checks=[]
        for species,series in data.items():
            labels=series['categories']
            for name in ['RegNetwork_v1','RegNetwork_core_v2','RegNetwork_v2']:
                counts=series[name]
                reported=counts[labels.index('Edge')] if 'Edge' in labels else None
                total=sum(v for label,v in zip(labels,counts) if '-' in label)
                checks.append({'species':species,'series':name,'reported_edges':reported,'relation_category_sum':total,'consistent':reported==total})
        warnings=['UPSTREAM_STATISTICS_INCONSISTENT'] if any(not c['consistent'] for c in checks) else []
        return self.envelope({'upstream_available':True,'availability_checked_routes':[observation.route],
                              'upstream_statistics':data,'statistics_checks':checks,'cache':self.cache.statistics(),
                              'available_tools':TOOLS,'local_full_snapshot_loaded':False,'pca_cmi_available':False},
                             {'refresh':refresh},[observation],status='partial' if warnings else 'ok',warnings=warnings)

    async def resolve(self,species,dataset,symbols,refresh=False):
        if not 1<=len(symbols)<=50:
            raise ApplicationError('QUERY_TOO_LARGE','resolve 1 to 50 symbols')
        symbols=list(dict.fromkeys(valid_symbol(s) for s in symbols))
        resolutions=[]; observations=[]; deadline=time.monotonic()+60
        for symbol in symbols:
            remaining=deadline-time.monotonic()
            if remaining<=0:
                resolutions.append({'symbol':symbol,'state':'not_processed','candidates':[],'candidate_scan_complete':False})
                continue
            try:
                scan=await self.adapter.scan(species,dataset,symbol,refresh=refresh,budget_seconds=min(60,remaining))
            except (UpstreamError,UpstreamFormatError) as exc:
                resolutions.append({'symbol':symbol,'state':'fetch_failed','candidates':[],'candidate_scan_complete':False,'error':str(exc)})
                continue
            resolution=resolve_scan(scan,symbol)
            observations.extend(scan.observations)
            self._remember_resolution(resolution,species,dataset,scan.observations)
            resolutions.append(resolution)
        incomplete=any(not r['candidate_scan_complete'] for r in resolutions)
        return self.envelope({'resolutions':resolutions},{'species':species,'dataset':dataset,'symbols':symbols,'refresh':refresh},observations,
                             status='partial' if incomplete else 'ok',warnings=['RESOLUTION_INCOMPLETE'] if incomplete else [],
                             coverage={'authority_verified':False,'alias_coverage':'not_exhaustive','biological_validation':'not_performed'})

    async def search(self,species,dataset,*,regulator_symbol=None,target_symbol=None,regulator_entity_id=None,target_entity_id=None,
                     regulator_type='all',target_type='all',confidence='all',min_score=None,limit=30,offset=0,refresh=False):
        source_given=regulator_symbol is not None or regulator_entity_id is not None
        symbol=self._selector(species,dataset,regulator_symbol if source_given else target_symbol,
                              regulator_entity_id if source_given else target_entity_id)
        counterpart=target_symbol if source_given else regulator_symbol
        counterpart_id=target_entity_id if source_given else regulator_entity_id
        if counterpart_id:
            counterpart=self._selector(species,dataset,counterpart,counterpart_id)
        if counterpart is not None:
            counterpart=valid_symbol(counterpart)
        scan=await self.adapter.scan(species,dataset,symbol,regulator_type=regulator_type,target_type=target_type,confidence=confidence,refresh=refresh)
        selected=select_interactions(scan,symbol=symbol,direction='outgoing' if source_given else 'incoming',
                                     entity_id=regulator_entity_id if source_given else target_entity_id,
                                     counterpart_symbol=counterpart,counterpart_entity_id=counterpart_id,min_score=min_score,limit=limit,offset=offset)
        resolution=resolve_scan(scan,symbol);self._remember_resolution(resolution,species,dataset,scan.observations)
        self._remember_records([parse_interaction(r['raw_record'],species) for r in selected['records']],species,dataset,scan.observations)
        parameters={'species':species,'dataset':dataset,'regulator_symbol':regulator_symbol,'target_symbol':target_symbol,
                    'regulator_entity_id':regulator_entity_id,'target_entity_id':target_entity_id,'min_score':min_score,
                    'regulator_type':regulator_type,'target_type':target_type,'confidence':confidence,'limit':limit,'offset':offset,'refresh':refresh}
        return self.envelope(selected,parameters,scan.observations,status=selected['status'],warnings=selected['warnings'])

    async def neighbors(self,species,dataset,*,symbol=None,entity_id=None,direction='both',limit=100,offset=0,refresh=False):
        symbol=self._selector(species,dataset,symbol,entity_id)
        scan=await self.adapter.scan(species,dataset,symbol,refresh=refresh)
        selected=select_interactions(scan,symbol=symbol,entity_id=entity_id,direction=direction,limit=limit,offset=offset)
        self._remember_resolution(resolve_scan(scan,symbol),species,dataset,scan.observations)
        self._remember_records([parse_interaction(r['raw_record'],species) for r in selected['records']],species,dataset,scan.observations)
        return self.envelope(selected,{'species':species,'dataset':dataset,'symbol':symbol,'entity_id':entity_id,'direction':direction,
                                       'limit':limit,'offset':offset,'refresh':refresh},scan.observations,status=selected['status'],warnings=selected['warnings'])

    async def subnetwork(self,species,dataset,symbols,*,max_edges=5000,refresh=False):
        symbols=list(dict.fromkeys(valid_symbol(s) for s in symbols))
        records,observation=await self.adapter.mapping(species,dataset,symbols,refresh=refresh)
        resolutions={}
        for symbol in symbols:
            # Only use an explicit previous resolution, never infer known isolation from a zero matrix.
            key=ResultCache.key('resolution',[species,dataset,symbol])
            hit=self.cache.get(key,allow_expired=True)
            if hit is not None:resolutions[symbol]=hit.payload
        network=extract_induced(records,symbols,resolutions=resolutions,max_edges=max_edges)
        parameters={'species':species,'dataset':dataset,'symbols':symbols,'mode':'induced','max_edges':max_edges,'refresh':refresh}
        network['provenance']={'query_parameters':parameters,'observations':[asdict(observation)],'source_release':'RegNetwork 2025 (site label)'}
        network_id='subnetwork_'+ResultCache.key('subnetwork-v1',network)
        if not self.cache.put(ResultCache.key('artifact',network_id),network,namespace='artifact'):
            raise ApplicationError('QUERY_TOO_LARGE','subnetwork exceeds local artifact budget')
        self._remember_records([parse_interaction(r['raw_record'],species) for r in network['records']],species,dataset,[observation])
        public={k:v for k,v in network.items() if k not in {'raw_records','records'}}
        public.update({'subnetwork_id':network_id,'record_preview':network['records'][:30],'record_preview_limit':30})
        return self.envelope(public,parameters,[observation],status='partial' if network['truncated'] else 'ok',warnings=network['warnings'])

    def _network(self,network_id):
        if not re.fullmatch(r'subnetwork_[0-9a-f]{64}',network_id):
            raise ApplicationError('ARTIFACT_NOT_FOUND','invalid subnetwork ID')
        hit=self.cache.get(ResultCache.key('artifact',network_id),allow_expired=True)
        if hit is None:raise ApplicationError('ARTIFACT_NOT_FOUND','subnetwork is absent, evicted or beyond retention; extract it again')
        return hit.payload

    async def summarize(self,network_id,include_self_loops=True):
        network=self._network(network_id)
        result=self.envelope(summarize_subnetwork(network,include_self_loops=include_self_loops),{'subnetwork_id':network_id,'include_self_loops':include_self_loops},
                             status='partial' if network['truncated'] else 'ok',warnings=network['warnings'])
        result.provenance['original_network']=network['provenance']
        return result

    async def evidence(self,interaction_id,max_references=20):
        if not 1<=max_references<=100:raise ValueError('invalid reference limit')
        hit=self.cache.get(ResultCache.key('interaction',interaction_id),allow_expired=True)
        if hit is None:raise ApplicationError('EVIDENCE_UNAVAILABLE','read this interaction through a query or mapping first; no score or PMID is fabricated')
        record=parse_interaction(hit.payload['raw'],hit.payload['species'])
        data={'interaction_id':record.interaction_id,'regulator':asdict(record.regulator),'target':asdict(record.target),
              'score':record.score,'confidence_raw':record.confidence_raw,'source_count_inferred':record.source_count_inferred,
              'source_databases_raw':record.source_databases_raw,'references':[asdict(r) for r in record.references[:max_references]],
              'references_raw':record.references_raw,'references_truncated':len(record.references)>max_references,
              'source_record_reviewed_by_expert':False,'paper_full_text_fetched':False}
        warnings=list(record.warnings)
        if hit.stale:warnings.append('HISTORICAL_CACHED_OBSERVATION')
        observations=[ReadObservation(**o) for o in hit.payload['observations']]
        return self.envelope(data,{'interaction_id':interaction_id,'max_references':max_references},observations,
                             status='partial' if warnings or data['references_truncated'] else 'ok',warnings=warnings,
                             coverage={'score_available':record.score is not None,'parsed_pmid_available':any(r.pmid for r in record.references),
                                       'biological_validation':'not_performed'})

    async def export(self,network_id,format_name):
        network=self._network(network_id)
        artifact=export_network(network,self.exports,format_name)
        identifier=Path(artifact['path']).stem.removeprefix('regnetwork-')
        artifact['resource_uri']='regnetwork://exports/'+identifier
        result=self.envelope(artifact,{'subnetwork_id':network_id,'format':format_name})
        result.provenance['original_network']=network['provenance']
        return result

    def read_export(self,identifier):
        if not re.fullmatch(r'[0-9a-f]{64}',identifier):
            raise ApplicationError('ARTIFACT_NOT_FOUND','invalid export ID')
        root=self.exports.resolve()
        for format_name in ['tsv','json','graphml']:
            path=root/f'regnetwork-{identifier}.{format_name}'
            manifest=root/f'regnetwork-{identifier}.manifest.json'
            if path.exists() and manifest.exists():
                if not path.resolve().is_relative_to(root) or not manifest.resolve().is_relative_to(root):
                    raise ApplicationError('EXPORT_PATH_INVALID','resource is outside configured export directory')
                content=path.read_bytes()
                expected=json.loads(manifest.read_text(encoding='utf-8'))
                if hashlib.sha256(content).hexdigest()!=expected['sha256']:
                    raise ApplicationError('EXPORT_INTEGRITY_ERROR','export was changed after creation')
                return content
        raise ApplicationError('ARTIFACT_NOT_FOUND','export file is absent')
