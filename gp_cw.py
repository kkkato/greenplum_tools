import psycopg2, psycopg2.extras

import random
import re
import argparse
from subprocess import Popen, PIPE
from datetime import datetime
import time
from multiprocessing.dummy import Pool


def get_cursor(config):
    conn = psycopg2.connect("dbname={database} user={user} host={host} port={port} password={password}".format(**config))
    conn.autocommit = False
    cursor =  conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return cursor

def out(cursor, sql, params = {}):
    # print(cursor.mogrify(sql, params).decode('utf-8'))
    cursor.execute(sql, params)
    return_val = {}
    try:
        return_val = cursor.fetchall()
    except psycopg2.ProgrammingError:
        pass
    return return_val

#TODO: calculate weights
QUICKLZ_1 = 1

ZLIB_1 = 2
ZLIB_5 = 3
ZLIB_9 = 4


RLE_TYPE_1 = 3
RLE_TYPE_2 = RLE_TYPE_1 + ZLIB_1
RLE_TYPE_3 = RLE_TYPE_1 + ZLIB_5
RLE_TYPE_4 = RLE_TYPE_1 + ZLIB_9

WEIGHTS = {
    'QUICKLZ_1': QUICKLZ_1,
    'ZLIB_1': ZLIB_1,
    'ZLIB_5': ZLIB_5,
    'ZLIB_9': ZLIB_9,
    'RLE_TYPE_1': RLE_TYPE_1,
    'RLE_TYPE_2': RLE_TYPE_2,
    'RLE_TYPE_3': RLE_TYPE_3,
    'RLE_TYPE_4': RLE_TYPE_4
}
compressions = {
    'RLE_TYPE': [1, 2, 3, 4],
    'ZLIB': [1, 5, 9],
    'QUICKLZ': [1]
}
def is_current_compression_method(original_column_info, column_info):
    return original_column_info.get('compresslevel', None) == column_info.get('compresslevel', None) and original_column_info.get('compresstype', '') == column_info.get('compresstype', '').lower()

def out_info(results, original_column_info):
    sorted_results = sorted(results, key=lambda k: k['size'])
    current_column = {'size': sorted_results[0]['size']}
    for column_info in sorted_results:
        if is_current_compression_method(original_column_info, column_info):
            current_column = column_info

    print('-----', original_column_info['column_name'], '-----')

    #TODO: suggest alter table alter column if it posible
    for column_info in sorted_results:
        current_text = ''
        if  column_info == current_column:
            current_text = '<<<CURRENT'
        if current_column:
            diff = str(round(100.0 / current_column['size'] * column_info['size'], 2)) + ' %'
            print('--', column_info['column_name'], column_info['compresstype'], column_info['compresslevel'], column_info['size_h'], diff, current_text)
        else:
            print('--', column_info['column_name'], column_info['compresstype'], column_info['compresslevel'], column_info['size_h'], current_text)

def bench_column(config, column):
    curr = get_cursor(config)
    results = []
    for compresstype, levels in compressions.items():
        for compresslevel in levels:
            SQL = """
                CREATE TABLE compres_test_table
                WITH (
                  appendonly=true,
                  orientation=column,
                  compresstype={compresstype},
                  compresslevel={compresslevel}
                )
                AS (SELECT {column_name} from {schema}.{table} LIMIT {lines})
            """.format(compresstype=compresstype,compresslevel=compresslevel, column_name=column['column_name'], schema=config['schema'], table=config['table'], lines=config['lines'])
            out(curr, SQL)

            SIZE_SQL = """
                SELECT
                '{column_name}' as column_name,
                '{compresslevel}' as compresslevel,
                '{compresstype}' as compresstype,
                pg_size_pretty(pg_relation_size('compres_test_table'::regclass::oid)) as size_h,
                '{attnum}' as attnum,
                pg_relation_size('compres_test_table'::regclass::oid) as size
            """.format(compresstype=compresstype, compresslevel=compresslevel, column_name=column['column_name'], attnum=column['attnum'])
            size_info = out(curr, SIZE_SQL)[0]
            results.append(size_info)
            out(curr, 'drop table compres_test_table')


    out_info(results, column)
    return results

def format_col(source_col):
    col = {
        'column_name': source_col['column_name'],
        'attnum': source_col['attnum']
    }
    opts = source_col.get('col_opts', [])

    if opts is None:
        return col

    for opt in opts:
        [param, value] = opt.split('=')
        col[param] = value.lower()
    return col

#chose best, need model
def get_best_column_format(column_info, config):
    sorted_results = sorted(column_info, key=lambda k: k['size'])
    best  = sorted_results[0] #first is the best
    competitors = []
    for column_info in sorted_results[1:]:
        if 100 * best['size'] / column_info['size'] >= config['tradeoff_treshold']:
            comp_key = '{compresstype}_{compresslevel}'.format(**column_info)
            column_info['weight'] = WEIGHTS.get(comp_key, 5)
            competitors.append(column_info)
    sorted_competitors_by_cost = sorted(competitors, key=lambda k: k['weight'])    
    return sorted_competitors_by_cost[0] if len(sorted_competitors_by_cost) else best

def make_magic(config):
    curr = get_cursor(config)
    TABLE_DESC_SQL = """
        SELECT a.attname as column_name,
        e.attoptions as col_opts,
        a.attnum as attnum
        FROM pg_catalog.pg_attribute a
        LEFT  JOIN pg_catalog.pg_attribute_encoding e ON  e.attrelid = a.attrelid AND e.attnum = a.attnum
        LEFT JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE  a.attnum > 0
        AND NOT a.attisdropped
        AND c.relname = %(table)s and n.nspname = %(schema)s
        ORDER BY a.attnum
    """
    table_info = out(curr, TABLE_DESC_SQL, config)

    thread_params = []

    for column in table_info:
        column = format_col(column)
        thread_params.append((config, column))

    results = Pool(config['threads']).starmap(bench_column, thread_params)
    sorted_as_source_table = sorted(results, key=lambda k: k[0]['attnum'])

    column_sqls = []
    for column_info in sorted_as_source_table:
        #TODO: chose compression by smart formula
        best_colum_format = get_best_column_format(column_info, config)
        sql = 'COLUMN {column_name} ENCODING (compresstype={compresstype}, COMPRESSLEVEL={compresslevel})'.format(**best_colum_format)
        column_sqls.append(sql)

    #TODO: create indexes if present
    SUGGESTED_SQL = """
        SET search_path TO {schema};

        CREATE TABLE {table}_new_type (
          LIKE {table},
          {columns}
        )
        WITH (
          appendonly=true,
          orientation=column,
          compresstype=RLE_TYPE,
          COMPRESSLEVEL=3
        );
        INSERT INTO {table}_new_type SELECT * FROM {table};
        ANALYZE {table}_new_type;


        --CHECK INDEXES
        BEGIN;
        ALTER TABLE {table} RENAME TO {table}_old;
        ALTER TABLE {table}_new_type RENAME TO {table};
        COMMIT;

    """.format(schema=config['schema'], table=config['table'], columns=',\n'.join(column_sqls))
    print(SUGGESTED_SQL)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--database", type=str, help="db name", default="db")
    parser.add_argument("--host", type=str, help="hostname", default="localhost")
    parser.add_argument("--port", type=int, help="port", default=5432)
    parser.add_argument("--user", type=str, help="username", default='gpadmin')
    parser.add_argument("--password", type=str, help="password")

    parser.add_argument("-t", "--table", type=str, help="table name", required=True)
    parser.add_argument("-s", "--schema", type=str, help="schema name", required=True)
    parser.add_argument("-l", "--lines", type=str, help="rows to examine", default=10000000)
    parser.add_argument("--threads", type=int, help="number of threads to run bench func", default=5)
    parser.add_argument("--tradeoff_treshold", type=int, help="compaction treshhold tradeofff %", default=90, choices=range(1, 100))

    params = parser.parse_args()
    make_magic(vars(params))
