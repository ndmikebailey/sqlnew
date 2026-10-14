/* Lossless nullable int64 blocks and predicate-pushing SQL scans. */
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT3
#include <stdint.h>
#include <string.h>

#define IB_HEADER 40
#define IB_LIMIT (64*1024*1024)
typedef unsigned char IbByte;
typedef sqlite3_int64 IbInt;
typedef sqlite3_uint64 IbUnsigned;
typedef struct IbEntry IbEntry;
struct IbEntry { IbInt value; int isNull; };
typedef struct IbBuilder IbBuilder;
struct IbBuilder { IbEntry *entries; int count,capacity,failed,nulls; IbInt min,max; };
static uint32_t ibGet32(const IbByte *p){
  return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static void ibPut32(IbByte *p,uint32_t v){
  p[0]=(IbByte)v; p[1]=(IbByte)(v>>8); p[2]=(IbByte)(v>>16); p[3]=(IbByte)(v>>24);
}
static IbInt ibGet64(const IbByte *p){
  IbUnsigned u=(IbUnsigned)ibGet32(p)|((IbUnsigned)ibGet32(p+4)<<32);
  IbInt value; memcpy(&value,&u,8); return value;
}
static void ibPut64(IbByte *p,IbInt value){
  IbUnsigned u=(IbUnsigned)value;
  ibPut32(p,(uint32_t)u); ibPut32(p+4,(uint32_t)(u>>32));
}
static uint32_t ibChecksum(const IbByte *p,int n){
  uint32_t a=1,b=0;
  int i;
  for(i=0;i<n;i++){
    if( i>=32 && i<36 ) continue;
    a+=p[i]; b+=a;
    if( (i&4095)==4095 ){ a%=65521; b%=65521; }
  }
  return ((b%65521)<<16)|(a%65521);
}
static int ibWidth(IbUnsigned span){
  int width=0;
  while( span ){ width++; span>>=1; }
  return width;
}
static void ibWriteBits(IbByte *p,IbUnsigned bit,int width,IbUnsigned value){
  int shift=(int)(bit&7),remaining=width;
  p+=(size_t)(bit>>3);
  while( remaining ){
    int take=8-shift;
    unsigned mask;
    if( take>remaining ) take=remaining;
    mask=(1u<<take)-1;
    *p++|=(IbByte)((value&mask)<<shift);
    value>>=take; remaining-=take; shift=0;
  }
}
static IbUnsigned ibReadBits(const IbByte *p,IbUnsigned bit,int width){
  IbUnsigned value=0;
  int shift=(int)(bit&7),collected=0,remaining=width;
  p+=(size_t)(bit>>3);
  while( remaining ){
    int take=8-shift;
    unsigned mask;
    if( take>remaining ) take=remaining;
    mask=(1u<<take)-1;
    value|=(IbUnsigned)((*p++>>shift)&mask)<<collected;
    collected+=take; remaining-=take; shift=0;
  }
  return value;
}
static void ibStep(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  IbBuilder *b=(IbBuilder*)sqlite3_aggregate_context(ctx,sizeof(*b));
  int kind=sqlite3_value_type(argv[0]);
  IbInt value=sqlite3_value_int64(argv[0]);
  (void)argc;
  if( !b ){ sqlite3_result_error_nomem(ctx); return; }
  if( b->failed ) return;
  if( kind!=SQLITE_NULL && kind!=SQLITE_INTEGER ){
    b->failed=1; sqlite3_result_error(ctx,"int_pack accepts only INTEGER or NULL",-1); return;
  }
  if( b->count>=IB_LIMIT/(int)sizeof(IbEntry) ){
    b->failed=1; sqlite3_result_error_toobig(ctx); return;
  }
  if( b->count==b->capacity ){
    int capacity=b->capacity ? b->capacity*2 : 256;
    IbEntry *entries=(IbEntry*)sqlite3_realloc64(b->entries,(IbUnsigned)capacity*sizeof(*entries));
    if( !entries ){ b->failed=1; sqlite3_result_error_nomem(ctx); return; }
    b->entries=entries; b->capacity=capacity;
  }
  b->entries[b->count].isNull=(kind==SQLITE_NULL);
  b->entries[b->count].value=value;
  if( kind==SQLITE_NULL ) b->nulls++;
  else if( b->count==b->nulls ) b->min=b->max=value;
  else{
    if( value<b->min ) b->min=value;
    if( value>b->max ) b->max=value;
  }
  b->count++;
}
static void ibFinal(sqlite3_context *ctx){
  IbBuilder *b=(IbBuilder*)sqlite3_aggregate_context(ctx,0);
  IbUnsigned size,span;
  IbByte *out,*bits;
  int width,nullBytes,i,limit;
  if( !b ) return;
  if( b->failed ){ sqlite3_free(b->entries); b->entries=0; return; }
  span=(IbUnsigned)b->max-(IbUnsigned)b->min;
  width=ibWidth(span); nullBytes=b->nulls ? (b->count+7)/8 : 0;
  size=IB_HEADER+(IbUnsigned)nullBytes+((IbUnsigned)b->count*width+7)/8;
  limit=sqlite3_limit(sqlite3_context_db_handle(ctx),SQLITE_LIMIT_LENGTH,-1);
  if( size>IB_LIMIT || size>(IbUnsigned)limit ){
    sqlite3_result_error_toobig(ctx); goto finish;
  }
  out=(IbByte*)sqlite3_malloc64(size);
  if( !out ){ sqlite3_result_error_nomem(ctx); goto finish; }
  memset(out,0,(size_t)size);
  memcpy(out,"SNI1",4); ibPut32(out+4,(uint32_t)b->count);
  ibPut64(out+8,b->min); ibPut64(out+16,b->max);
  ibPut32(out+24,(uint32_t)width); ibPut32(out+28,(uint32_t)b->nulls);
  bits=out+IB_HEADER+nullBytes;
  for(i=0;i<b->count;i++){
    if( b->entries[i].isNull ) out[IB_HEADER+i/8]|=(IbByte)(1u<<(i&7));
    else ibWriteBits(bits,(IbUnsigned)i*width,width,(IbUnsigned)b->entries[i].value-(IbUnsigned)b->min);
  }
  ibPut32(out+32,ibChecksum(out,(int)size));
  sqlite3_result_blob(ctx,out,(int)size,sqlite3_free);
finish:
  sqlite3_free(b->entries); b->entries=0;
}

typedef struct IbTable IbTable;
struct IbTable { sqlite3_vtab base; sqlite3 *db; };
typedef struct IbCursor IbCursor;
struct IbCursor {
  sqlite3_vtab_cursor base;
  sqlite3_value *block;
  const IbByte *bits,*nullmap;
  IbInt min,max,lower,upper,value;
  int count,pos,width,hasLower,hasUpper,strictLower,strictUpper,isNull,empty;
};
static int ibConnect(sqlite3 *db,void *aux,int argc,const char *const*argv,sqlite3_vtab **out,char **error){
  IbTable *t;
  int rc;
  (void)aux; (void)argc; (void)argv; (void)error;
  rc=sqlite3_declare_vtab(db,"CREATE TABLE x(value INTEGER,ordinal INTEGER,block HIDDEN)");
  if( rc ) return rc;
  t=(IbTable*)sqlite3_malloc64(sizeof(*t));
  if( !t ) return SQLITE_NOMEM;
  memset(t,0,sizeof(*t)); t->db=db; *out=&t->base; return SQLITE_OK;
}
static int ibDisconnect(sqlite3_vtab *p){ sqlite3_free(p); return SQLITE_OK; }
static int ibBest(sqlite3_vtab *v,sqlite3_index_info *p){
  int i,block=-1,n=0;
  char *ops;
  (void)v;
  for(i=0;i<p->nConstraint;i++){
    if( p->aConstraint[i].iColumn==2 && p->aConstraint[i].usable && p->aConstraint[i].op==SQLITE_INDEX_CONSTRAINT_EQ ){
      block=i; break;
    }
  }
  if( block<0 ) return SQLITE_CONSTRAINT;
  p->aConstraintUsage[block].argvIndex=1; p->aConstraintUsage[block].omit=1;
  ops=(char*)sqlite3_malloc64((IbUnsigned)p->nConstraint+1);
  if( !ops ) return SQLITE_NOMEM;
  for(i=0;i<p->nConstraint;i++){
    int op=p->aConstraint[i].op;
    if( p->aConstraint[i].iColumn!=0 || !p->aConstraint[i].usable ) continue;
    if( op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_GT || op==SQLITE_INDEX_CONSTRAINT_GE
        || op==SQLITE_INDEX_CONSTRAINT_LT || op==SQLITE_INDEX_CONSTRAINT_LE ){
      ops[n++]=(char)op; p->aConstraintUsage[i].argvIndex=n+1;
      /* Recheck in SQLite to preserve affinity rules for noninteger bounds. */
    }
  }
  ops[n]=0; p->idxStr=ops; p->needToFreeIdxStr=1;
  p->estimatedCost=500; p->estimatedRows=n ? 100 : 1000;
  if( p->nOrderBy==1 && p->aOrderBy[0].iColumn==1 && !p->aOrderBy[0].desc ) p->orderByConsumed=1;
  return SQLITE_OK;
}
static int ibOpen(sqlite3_vtab *t,sqlite3_vtab_cursor **out){
  IbCursor *c=(IbCursor*)sqlite3_malloc64(sizeof(*c));
  if( !c ) return SQLITE_NOMEM;
  memset(c,0,sizeof(*c)); c->base.pVtab=t; *out=&c->base; return SQLITE_OK;
}
static int ibClose(sqlite3_vtab_cursor *p){
  IbCursor *c=(IbCursor*)p; sqlite3_value_free(c->block); sqlite3_free(c); return SQLITE_OK;
}
static IbInt ibValue(IbCursor *c,int pos){
  IbUnsigned u=(IbUnsigned)c->min+ibReadBits(c->bits,(IbUnsigned)pos*c->width,c->width);
  IbInt value; memcpy(&value,&u,8); return value;
}
static int ibNext(sqlite3_vtab_cursor *p){
  IbCursor *c=(IbCursor*)p;
  while( ++c->pos<c->count ){
    if( (c->pos&1023)==0 && sqlite3_is_interrupted(((IbTable*)p->pVtab)->db) ) return SQLITE_INTERRUPT;
    c->isNull=c->nullmap && (c->nullmap[c->pos/8]&(1u<<(c->pos&7)));
    if( c->isNull ){
      if( !c->hasLower && !c->hasUpper ) break;
      continue;
    }
    c->value=ibValue(c,c->pos);
    if( c->hasLower && (c->value<c->lower || (c->strictLower && c->value==c->lower)) ) continue;
    if( c->hasUpper && (c->value>c->upper || (c->strictUpper && c->value==c->upper)) ) continue;
    break;
  }
  return SQLITE_OK;
}
static int ibFilter(sqlite3_vtab_cursor *p,int index,const char *ops,int argc,sqlite3_value **argv){
  IbCursor *c=(IbCursor*)p;
  IbTable *t=(IbTable*)p->pVtab;
  const IbByte *blob;
  int bytes,i,seenNull=0,seenValue=0,nullBytes;
  uint32_t count,nulls,width;
  IbInt actualMin=0,actualMax=0;
  IbUnsigned span,size;
  (void)index;
  sqlite3_value_free(c->block);
  memset((char*)c+sizeof(c->base),0,sizeof(*c)-sizeof(c->base));
  c->pos=-1;
  if( argc<1 ) return SQLITE_MISUSE;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return SQLITE_OK;
  bytes=sqlite3_value_bytes(argv[0]);
  if( sqlite3_value_type(argv[0])!=SQLITE_BLOB || bytes<IB_HEADER || bytes>IB_LIMIT ) goto bad;
  blob=(const IbByte*)sqlite3_value_blob(argv[0]);
  if( !blob ) return SQLITE_NOMEM;
  if( memcmp(blob,"SNI1",4) || ibGet32(blob+36) || ibGet32(blob+32)!=ibChecksum(blob,bytes) ) goto bad;
  count=ibGet32(blob+4); width=ibGet32(blob+24); nulls=ibGet32(blob+28);
  if( !count || count>IB_LIMIT/(int)sizeof(IbEntry) || width>64 || nulls>count ) goto bad;
  c->min=ibGet64(blob+8); c->max=ibGet64(blob+16);
  if( c->max<c->min ) goto bad;
  span=(IbUnsigned)c->max-(IbUnsigned)c->min;
  if( ibWidth(span)!=(int)width ) goto bad;
  nullBytes=nulls ? (int)(count+7)/8 : 0;
  size=IB_HEADER+(IbUnsigned)nullBytes+((IbUnsigned)count*width+7)/8;
  if( size!=(IbUnsigned)bytes ) goto bad;
  c->block=sqlite3_value_dup(argv[0]);
  if( !c->block ) return SQLITE_NOMEM;
  blob=(const IbByte*)sqlite3_value_blob(c->block);
  c->bits=blob+IB_HEADER+nullBytes; c->nullmap=nulls ? blob+IB_HEADER : 0;
  c->count=(int)count; c->width=(int)width;
  /* Validate metadata before using it as a pruning fact. No trust in a blob's
  ** claimed minimum, maximum, or null count is required. */
  for(i=0;i<c->count;i++){
    IbUnsigned offset=ibReadBits(c->bits,(IbUnsigned)i*width,(int)width);
    IbInt value;
    if( (i&1023)==0 && sqlite3_is_interrupted(t->db) ) return SQLITE_INTERRUPT;
    if( c->nullmap && (c->nullmap[i/8]&(1u<<(i&7))) ){
      if( offset ) goto bad;
      seenNull++; continue;
    }
    if( offset>span ) goto bad;
    value=ibValue(c,i);
    if( !seenValue ){ actualMin=actualMax=value; seenValue=1; }
    else { if( value<actualMin ) actualMin=value; if( value>actualMax ) actualMax=value; }
  }
  if( seenNull!=(int)nulls || actualMin!=c->min || actualMax!=c->max ) goto bad;
  for(i=1;i<argc;i++){
    int op=(unsigned char)ops[i-1],kind=sqlite3_value_type(argv[i]);
    IbInt value;
    if( kind==SQLITE_NULL ){ c->empty=1; continue; }
    if( kind!=SQLITE_INTEGER ) continue;
    value=sqlite3_value_int64(argv[i]);
    if( op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_GT || op==SQLITE_INDEX_CONSTRAINT_GE ){
      int strict=(op==SQLITE_INDEX_CONSTRAINT_GT);
      if( !c->hasLower || value>c->lower ) { c->lower=value; c->strictLower=strict; }
      else if( value==c->lower ) c->strictLower|=strict;
      c->hasLower=1;
    }
    if( op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_LT || op==SQLITE_INDEX_CONSTRAINT_LE ){
      int strict=(op==SQLITE_INDEX_CONSTRAINT_LT);
      if( !c->hasUpper || value<c->upper ) { c->upper=value; c->strictUpper=strict; }
      else if( value==c->upper ) c->strictUpper|=strict;
      c->hasUpper=1;
    }
  }
  if( c->empty || (c->hasLower && (c->lower>c->max || (c->strictLower && c->lower==c->max)))
      || (c->hasUpper && (c->upper<c->min || (c->strictUpper && c->upper==c->min))) ) c->count=0;
  return ibNext(p);
bad:
  c->count=0;
  sqlite3_free(t->base.zErrMsg); t->base.zErrMsg=sqlite3_mprintf("invalid SQLNEW integer block");
  return SQLITE_ERROR;
}
static int ibEof(sqlite3_vtab_cursor *p){ IbCursor *c=(IbCursor*)p; return c->pos>=c->count || c->count==0; }
static int ibColumn(sqlite3_vtab_cursor *p,sqlite3_context *ctx,int column){
  IbCursor *c=(IbCursor*)p;
  if( column==0 && !c->isNull ) sqlite3_result_int64(ctx,c->value);
  else if( column==1 ) sqlite3_result_int64(ctx,(IbInt)c->pos+1);
  else if( column==2 ) sqlite3_result_value(ctx,c->block);
  return SQLITE_OK;
}
static int ibRowid(sqlite3_vtab_cursor *p,IbInt *rowid){ *rowid=(IbInt)((IbCursor*)p)->pos+1; return SQLITE_OK; }
static sqlite3_module ibModule={
  0,0,ibConnect,ibBest,ibDisconnect,0,ibOpen,ibClose,ibFilter,ibNext,ibEof,ibColumn,ibRowid,
  0,0,0,0,0,0,0,0,0,0,0,0
};
int sqlnew_int_init(sqlite3 *db){
  int rc=sqlite3_create_function(db,"sqlnew_int_pack",1,SQLITE_UTF8|SQLITE_DETERMINISTIC|SQLITE_INNOCUOUS,0,0,ibStep,ibFinal);
  if( rc==SQLITE_OK ) rc=sqlite3_create_module(db,"sqlnew_int_scan",&ibModule,0);
  return rc;
}
