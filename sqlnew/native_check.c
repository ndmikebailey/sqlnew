/* Allocation-failure and malformed-input checks for the native engine. */
#include "sqlite3.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static sqlite3_mem_methods original;
static int countdown=-1, faults=0;
static uint32_t randomState=20260905;
static uint32_t randomWord(void){
  randomState^=randomState<<13; randomState^=randomState>>17;
  randomState^=randomState<<5; return randomState;
}
static int shouldFail(void){
  if( countdown<0 ) return 0;
  if( countdown--==0 ){ countdown=-1; faults++; return 1; }
  return 0;
}
static void *checkedMalloc(int n){ return shouldFail() ? 0 : original.xMalloc(n); }
static void *checkedRealloc(void *p,int n){ return shouldFail() ? 0 : original.xRealloc(p,n); }
static int sql(sqlite3 *db,const char *text){
  char *error=0;
  int rc=sqlite3_exec(db,text,0,0,&error);
  sqlite3_free(error); return rc;
}
static int openDatabase(sqlite3 **db){
  int rc=sqlite3_open(":memory:",db);
  if( rc ) return rc;
  return sql(*db,
    "CREATE TABLE v(id INTEGER PRIMARY KEY,x BLOB);"
    "CREATE VIRTUAL TABLE coldata USING sqlnew_columnar(a INTEGER,b TEXT,c REAL,d BLOB);"
    "WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<80)"
    " INSERT INTO v SELECT i,x'0000803f000000400000404000008040' FROM n;");
}
static int traceSql(unsigned mask,void *aux,void *statement,void *text){
  (void)mask; (void)aux; (void)statement;
  fprintf(stderr,"TRACE %s\n",(const char*)text); return 0;
}
int main(void){
  sqlite3_mem_methods methods;
  sqlite3 *db=0;
  sqlite3_stmt *decode=0,*scan=0,*integers=0;
  int trial,rc,i,first=0,last=6000;
  const char *at=getenv("SQLNEW_FAULT_AT");
  const char *traceAt=getenv("SQLNEW_TRACE_AT");
  unsigned char bytes[2048];
  const char *work=
    "SELECT sqlnew_unpack(sqlnew_pack(printf('%.*c',12000,'a')));"
    "SELECT count(*) FROM sqlnew_topk((SELECT sqlnew_vector_block(id,x,0) FROM v),"
    "x'0000803f000000400000404000008040',13);"
    "SELECT count(*) FROM sqlnew_topk((SELECT sqlnew_vector_block(id,x,1) FROM v),"
    "x'0000803f000000400000404000008040',13);"
    "SELECT sum(value) FROM sqlnew_int_scan((SELECT sqlnew_int_pack(CASE WHEN id%7=0 THEN NULL ELSE id END) FROM v)) WHERE value BETWEEN 2 AND 71;"
    "BEGIN;"
    "INSERT INTO coldata SELECT id,printf('record-%d',id%9),id*1.5,x FROM v;"
    "SAVEPOINT c;"
    "UPDATE coldata SET a=-a WHERE rowid IN (2,5,9);"
    "DELETE FROM coldata WHERE rowid=3;"
    "SELECT count(*),sum(a),min(b),max(b) FROM coldata;"
    "SELECT a,b,c,d FROM coldata WHERE a>=3;"
    "SELECT b,count(*),sum(a),avg(c) FROM coldata GROUP BY b ORDER BY b;"
    "INSERT INTO coldata(__sqlnew_command) VALUES('{\"create_index\":\"fault_index\",\"keys\":[\"a\"],\"include\":[\"c\"]}');"
    "UPDATE coldata SET a=1000,c=9.0 WHERE rowid=4;"
    "SELECT count(*),sum(c) FROM coldata WHERE a>=100;"
    "INSERT INTO coldata(__sqlnew_command) VALUES('compact');"
    "PRAGMA integrity_check;"
    "INSERT INTO coldata(__sqlnew_command) VALUES('{\"drop_index\":\"fault_index\"}');"
    "ROLLBACK TO c; RELEASE c; COMMIT;";
  if( sqlite3_config(SQLITE_CONFIG_GETMALLOC,&original)!=SQLITE_OK ) return 1;
  methods=original; methods.xMalloc=checkedMalloc; methods.xRealloc=checkedRealloc;
  if( sqlite3_config(SQLITE_CONFIG_MALLOC,&methods)!=SQLITE_OK ) return 2;
  if( at ){ first=atoi(at); last=first+1; }
  for(trial=first;trial<last;trial++){
    countdown=-1;
    if( openDatabase(&db)!=SQLITE_OK ) return 3;
    if( at || (traceAt && trial==atoi(traceAt)) ) sqlite3_trace_v2(db,SQLITE_TRACE_STMT,traceSql,0);
    countdown=trial;
    rc=sql(db,work);
    countdown=-1;
    if( rc!=SQLITE_OK && rc!=SQLITE_NOMEM ){
      fprintf(stderr,"unexpected fault result %d at allocation %d: %s\n",rc,trial,sqlite3_errmsg(db)); return 4;
    }
    if( sqlite3_close(db)!=SQLITE_OK ) return 5;
  }
  if( openDatabase(&db)!=SQLITE_OK ) return 6;
  if( sqlite3_prepare_v2(db,"SELECT sqlnew_unpack(?)",-1,&decode,0)!=SQLITE_OK ) return 7;
  if( sqlite3_prepare_v2(db,"SELECT * FROM sqlnew_topk(?,x'0000803f000000400000404000008040',7)",-1,&scan,0)!=SQLITE_OK ) return 8;
  if( sqlite3_prepare_v2(db,"SELECT * FROM sqlnew_int_scan(?) WHERE value>=3",-1,&integers,0)!=SQLITE_OK ) return 8;
  for(trial=0;trial<20000;trial++){
    int length=(int)(randomWord()%sizeof(bytes));
    for(i=0;i<length;i++) bytes[i]=(unsigned char)randomWord();
    if( length>=16 && (trial&1) ){
      memcpy(bytes,"SNZ1",4);
      bytes[4]=(unsigned char)randomWord(); bytes[5]=(unsigned char)(randomWord()%4);
      bytes[6]=0; bytes[7]=0; bytes[12]=(unsigned char)(trial%2);
      bytes[13]=bytes[14]=bytes[15]=0;
    }
    sqlite3_bind_blob(decode,1,bytes,length,SQLITE_TRANSIENT);
    (void)sqlite3_step(decode); sqlite3_reset(decode);
    if( length>=16 && (trial%3)==0 ){
      memcpy(bytes,"SNV1",4);
      bytes[4]=4; bytes[5]=bytes[6]=bytes[7]=0;
      bytes[12]=(unsigned char)(trial&1); bytes[13]=bytes[14]=bytes[15]=0;
    }
    sqlite3_bind_blob(scan,1,bytes,length,SQLITE_TRANSIENT);
    while( sqlite3_step(scan)==SQLITE_ROW ){}
    sqlite3_reset(scan);
    sqlite3_bind_blob(integers,1,bytes,length,SQLITE_TRANSIENT);
    while( sqlite3_step(integers)==SQLITE_ROW ){}
    sqlite3_reset(integers);
  }
  sqlite3_finalize(decode); sqlite3_finalize(scan); sqlite3_finalize(integers);
  if( sqlite3_close(db)!=SQLITE_OK ) return 9;
  sqlite3_shutdown();
  if( sqlite3_memory_used()!=0 ){
    fprintf(stderr,"SQLite allocations still live: %lld\n",sqlite3_memory_used()); return 10;
  }
  printf("OK: %d allocation-failure positions (%d injected faults), 20000 malformed frames, zero live SQLite allocations\n",last-first,faults);
  return 0;
}
