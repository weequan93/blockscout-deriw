"""Catalog review for named collations whose recorded library versions differ."""
import dev_config as D

FORMAT = 'deriw-v10-collation-review-v1'
COUNTS = ('direct_dependencies', 'pinned_mismatched_collations',
          'relation_attributes_using_mismatches', 'index_keys_using_mismatches',
          'partition_keys_using_mismatches', 'types_using_mismatches',
          'ranges_using_mismatches', 'user_functions_mentioning_collate')

CTES = r"""
WITH collation_bad AS MATERIALIZED (
 SELECT oid FROM pg_collation
 WHERE collversion IS DISTINCT FROM pg_collation_actual_version(oid)
), collation_refs AS MATERIALIZED (
 SELECT d.* FROM pg_depend d JOIN collation_bad b
 ON d.refclassid='pg_collation'::regclass AND d.refobjid=b.oid
)
"""

REVIEW_JSON = r"""jsonb_build_object(
 'format','deriw-v10-collation-review-v1',
 'default_collation_mismatch',(SELECT datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)
  FROM pg_database WHERE datname=current_database()),
 'mismatched_collations',(SELECT count(*) FROM collation_bad),
 'direct_dependencies',(SELECT count(*) FROM collation_refs),
 'pinned_mismatched_collations',(SELECT count(*) FROM collation_refs WHERE deptype='p'),
 'dependency_catalogs',(SELECT coalesce(jsonb_agg(to_jsonb(g)),'[]'::jsonb) FROM (
  SELECT classid::regclass::text AS catalog,count(*) AS count FROM collation_refs GROUP BY classid ORDER BY classid) g),
 'relation_attributes_using_mismatches',(SELECT count(*) FROM pg_attribute a JOIN collation_bad b ON b.oid=a.attcollation WHERE NOT a.attisdropped),
 'index_keys_using_mismatches',(SELECT count(*) FROM pg_index i CROSS JOIN LATERAL unnest(i.indcollation) k(oid) JOIN collation_bad b ON b.oid=k.oid),
 'partition_keys_using_mismatches',(SELECT count(*) FROM pg_partitioned_table p CROSS JOIN LATERAL unnest(p.partcollation) k(oid) JOIN collation_bad b ON b.oid=k.oid),
 'types_using_mismatches',(SELECT count(*) FROM pg_type t JOIN collation_bad b ON b.oid=t.typcollation),
 'ranges_using_mismatches',(SELECT count(*) FROM pg_range r JOIN collation_bad b ON b.oid=r.rngcollation),
 'user_functions_mentioning_collate',(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosrc ~* '\mcollate\M')
)"""

QUERY = CTES + 'SELECT ' + REVIEW_JSON


def validate(data):
    D.require(data.get('database_collation_mismatch') is False,
              'Database default collation mismatch requires a separate object/index review')
    count = data.get('collation_mismatches')
    D.require(type(count) is int and count >= 0, 'Collation mismatch count is unavailable')
    if count == 0:
        return
    review = data.get('collation_review') or {}
    D.require(review.get('format') == FORMAT and review.get('default_collation_mismatch') is False
              and type(review.get('mismatched_collations')) is int
              and review['mismatched_collations'] == count
              and review.get('dependency_catalogs') == []
              and all(type(review.get(key)) is int and review[key] == 0 for key in COUNTS),
              'Named collation mismatches require a complete zero-dependency review; referenced or unresolved objects need separate review')
