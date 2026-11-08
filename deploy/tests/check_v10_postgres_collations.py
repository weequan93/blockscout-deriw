#!/usr/bin/env python3
"""Exercise collation review on owned local PG15 fixture data, including binary patches."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/scripts'))
import dev_config as D
import v10_infrastructure_checks as K
import check_v10_infrastructure as T


def main():
    f = T.Fixture()
    try:
        f.setup()
        volume = f.volume('collation-review')
        f.run(['docker','run','--rm','--pull','never','--network','none','--user','0:0',
               '--label',T.G.LABEL+'='+f.token,'--mount','type=volume,source='+volume+',target=/var/lib/postgresql/data',
               '--entrypoint','sh',T.OLD_PG,'-ec','chown 2000:2000 /var/lib/postgresql/data; chmod 700 /var/lib/postgresql/data'])
        f.pg_start(T.OLD_PG, volume)
        def observe():
            return json.loads(f.sql(K.PG_QUERY + ';'))
        def stopped():
            try:
                K.validate_postgres(observe())
            except D.ConfigError:
                return
            raise AssertionError('A used or default collation mismatch was incorrectly admitted')
        clean = observe()
        K.validate_postgres(clean)
        f.sql("CREATE COLLATION fixture_unused(provider=libc,locale='en_US.utf8'); UPDATE pg_collation SET collversion='synthetic-old-library' WHERE collname='fixture_unused';")
        unused = observe()
        assert unused['collation_mismatches'] == 1
        K.validate_postgres(unused)
        f.sql('CREATE TABLE fixture_used(t text COLLATE fixture_unused); CREATE INDEX fixture_used_idx ON fixture_used(t);')
        used = observe()['collation_review']
        assert used['direct_dependencies'] > 0 and used['index_keys_using_mismatches'] == 1
        stopped()
        f.sql('DROP TABLE fixture_used; CREATE TABLE fixture_partition(t text COLLATE fixture_unused) PARTITION BY RANGE(t);')
        assert observe()['collation_review']['partition_keys_using_mismatches'] == 1
        stopped()
        f.sql("DROP TABLE fixture_partition; CREATE FUNCTION fixture_source(text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT $1 COLLATE fixture_unused $$;")
        assert observe()['collation_review']['user_functions_mentioning_collate'] == 1
        stopped()
        f.sql('DROP FUNCTION fixture_source(text);')
        f.sql("UPDATE pg_database SET datcollversion='synthetic-old-library' WHERE datname=current_database();")
        stopped()
        f.sql('ALTER DATABASE fixture REFRESH COLLATION VERSION;')
        baseline = observe()
        K.validate_postgres(baseline)
        f.pg_stop()
        f.pg_start(K.catalog()['postgresql']['reference'], volume, guard=True)
        patched = observe()
        K.validate_postgres(patched, baseline, allow_minor=True)
        assert patched['version'] == 150019 and patched['collation_mismatches'] == 1
        assert f.sql("SELECT collversion FROM pg_collation WHERE collname='fixture_unused';").strip() == 'synthetic-old-library'
        f.pg_stop()
        f.pg_start(T.OLD_PG, volume, guard=True)
        rollback = observe()
        K.validate_postgres(rollback, baseline)
        result = {'status':'passed','live_services_accessed':False,
                  'unused_mismatch_admitted':True,'default_mismatch_still_stops':True,
                  'used_index_column_partition_and_sql_function_still_stop':True,
                  'actual_production_query_and_validator_used':True,
                  'same_cluster_oid_libraries_and_unused_metadata_preserved':True,
                  'pg15_18_to_15_19_and_rollback_passed':True,
                  'production_collation_refresh_or_reindex':False}
    finally:
        f.cleanup()
    result['fixture_resources_removed'] = True
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
