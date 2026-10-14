import csv
import hashlib
import json
from pathlib import Path
import networkx as nx
import pytest

from regnetwork_mcp.analysis import extract_induced
from regnetwork_mcp.cache import ResultCache
from regnetwork_mcp.exports import export_network
from regnetwork_mcp.records import parse_interaction


def test_cache_fresh_stale_explicit_and_retention(tmp_path):
    now = [0.0]
    cache = ResultCache(tmp_path/'cache.sqlite',ttl=10,retention=20,clock=lambda:now[0])
    cache.put('key',{'value':1})
    assert cache.get('key').payload == {'value':1}
    now[0] = 11
    assert cache.get('key') is None
    assert cache.get('key',allow_expired=True).stale
    now[0] = 21
    assert cache.get('key',allow_expired=True) is None


def test_cache_identity_includes_species_dataset_and_parameters():
    first = ResultCache.key('online-v1',{'species':'human','dataset':'core','symbol':'TP53'})
    assert first == ResultCache.key('online-v1',{'symbol':'TP53','dataset':'core','species':'human'})
    assert first != ResultCache.key('online-v1',{'species':'mouse','dataset':'core','symbol':'TP53'})
    assert first != ResultCache.key('online-v1',{'species':'human','dataset':'complete','symbol':'TP53'})


def test_cache_budget_eviction_and_oversized_payload(tmp_path):
    now=[0.0]
    cache=ResultCache(tmp_path/'cache.sqlite',max_bytes=100,clock=lambda:now[0])
    cache.put('first','a'*40);now[0]=1;cache.put('second','b'*40)
    now[0]=2;cache.get('first');now[0]=3;cache.put('third','c'*40)
    assert cache.get('first') is not None and cache.get('second') is None
    assert cache.statistics()['payload_bytes'] <= 100
    assert not cache.put('huge','x'*200)


def test_cache_survives_restart_and_does_not_reset_original_creation(tmp_path):
    path=tmp_path/'cache.sqlite'
    first=ResultCache(path,clock=lambda:100)
    first.put('key',{'text':'生物医学'})
    restarted=ResultCache(path,clock=lambda:200)
    hit=restarted.get('key')
    assert hit.created_at==100 and hit.payload['text']=='生物医学'


@pytest.fixture
def network():
    def edge(target,identifier):
        return parse_interaction('\t'.join(['TP53','NCBI:7157',target,identifier,'TF','Gene','core','High','1','TRRUST','0.8','123(Experimental Research)']),'human')
    # Parallel observations must not disappear from GraphML exports.
    graph=extract_induced([edge('BAX','NCBI:581'),edge('BAX','NCBI:581'),edge('TP53','NCBI:7157')],['TP53','BAX'])
    graph['provenance']={'source':'fixture','query_parameters':{'species':'human','dataset':'core'}}
    return graph


@pytest.mark.parametrize('format_name',['tsv','json','graphml'])
def test_export_roundtrip_direction_parallel_records_and_checksum(tmp_path,network,format_name):
    artifact=export_network(network,tmp_path,format_name)
    path=Path(artifact['path'])
    assert hashlib.sha256(path.read_bytes()).hexdigest()==artifact['sha256']
    manifest=json.loads(Path(artifact['manifest_path']).read_text())
    assert manifest['raw_record_count']==3 and manifest['directed']
    assert not manifest['activation_inhibition_sign_available']
    if format_name=='json':
        assert len(json.loads(path.read_text())['records'])==3
    elif format_name=='tsv':
        with path.open(encoding='utf-8',newline='') as stream:
            rows=list(csv.DictReader(stream,delimiter='\t'))
        assert len(rows)==3 and rows[0]['references_raw']=='123(Experimental Research)'
    else:
        graph=nx.read_graphml(path,force_multigraph=True)
        assert graph.is_directed() and graph.number_of_edges()==3
        assert nx.number_of_selfloops(graph)==1


def test_repeated_export_is_identical_and_does_not_overwrite_conflicting_file(tmp_path,network):
    first=export_network(network,tmp_path,'json')
    second=export_network(network,tmp_path,'json')
    assert first==second
    Path(first['path']).write_text('unrelated data')
    with pytest.raises(FileExistsError):
        export_network(network,tmp_path,'json')
    assert Path(first['path']).read_text()=='unrelated data'


def test_export_rejects_user_chosen_path_as_format(tmp_path,network):
    with pytest.raises(ValueError):
        export_network(network,tmp_path,'../unsafe')


def test_score_and_reference_missing_values_are_not_filled(tmp_path):
    record=parse_interaction('\t'.join(['TP53','NCBI:7157','BAX','NCBI:581','TF','Gene','core','N/A','1','TRRUST','N/A','N/A']),'human')
    network=extract_induced([record],['TP53','BAX'])
    artifact=export_network(network,tmp_path,'graphml')
    graph=nx.read_graphml(artifact['path'])
    attrs=next(iter(graph.edges(data=True)))[2]
    assert 'score' not in attrs and attrs['references_raw']=='N/A'
