/* Shared immutable region payloads with transactional, coordinate-local edits. */
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT3
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <float.h>

typedef sqlite3_int64 RgInt;
typedef sqlite3_uint64 RgWord;
typedef unsigned char RgByte;
#define RG_CELLS 512
#define RG_HEAD 16
#define RG_MAP 64
#define RG_RAW (RG_HEAD+2*RG_MAP+8*RG_CELLS)
#define RG_STMTS 10
extern int sqlnew_pack_buffer(sqlite3*,const void*,int,void**,int*);
extern int sqlnew_unpack_buffer(sqlite3*,const void*,int,void**,int*);
extern int sqlnew_fold_pack(const void*,int,RgWord,void**,int*);
extern int sqlnew_fold_unpack(const void*,int,void**,int*);

typedef struct RgLeaf RgLeaf;
struct RgLeaf { RgByte live[RG_MAP],nulls[RG_MAP]; RgWord words[RG_CELLS]; };
typedef struct RgTable RgTable;
typedef struct RgRegistry RgRegistry;
struct RgRegistry { RgTable *tables; unsigned refs; };
struct RgTable {
  sqlite3_vtab base;
  sqlite3 *db;
  RgRegistry *registry;
  RgTable *next;
  char *schema,*name;
  RgInt n[3],groups[3],total;
  int kind,busy;
  sqlite3_stmt *write[RG_STMTS];
  RgInt cacheId;
  RgLeaf cache;
  RgWord blocksRead,leavesDecoded,patchesRead,patchesWritten;
  RgWord objectsRead,bytesRead,rowsOut,baseWrites,baseReuse,compactions;
};
typedef struct RgCursor RgCursor;
struct RgCursor {
  sqlite3_vtab_cursor base;
  sqlite3_stmt *blocks,*patch,*node,*object;
  RgInt lo[3],hi[3],rowLo,rowHi,block,nodeId,decodedId,origin[3],rowid;
  RgWord anchor,patchBits[RG_CELLS];
  RgByte patched[RG_CELLS],patchState[RG_CELLS];
  RgLeaf leaf;
  int slot,eof,point,loaded;
};

static RgWord rgGet(const RgByte *p){
  RgWord v=0; int i;
  for(i=7;i>=0;i--) v=(v<<8)|p[i];
  return v;
}
static void rgPut(RgByte *p,RgWord v){
  int i; for(i=0;i<8;i++){ p[i]=(RgByte)v; v>>=8; }
}
static RgInt rgSigned(RgWord w){ RgInt v; memcpy(&v,&w,8); return v; }
static int rgBit(const RgByte *p,int i){ return (p[i>>3]>>(i&7))&1; }
static void rgSet(RgByte *p,int i){ p[i>>3]|=(RgByte)(1u<<(i&7)); }
static void rgRelease(void *data){
  RgRegistry *r=(RgRegistry*)data;
  if( --r->refs==0 ) sqlite3_free(r);
}
static void rgReset(sqlite3_stmt *s){ sqlite3_reset(s); sqlite3_clear_bindings(s); }
static int rgPrepare(RgTable *t,sqlite3_stmt **out,const char *object,const char *format){
  char *q=sqlite3_mprintf("\"%w\".\"%w_%w\"",t->schema,t->name,object);
  char *sql=q ? sqlite3_mprintf(format,q) : 0;
  int rc=sql ? sqlite3_prepare_v2(t->db,sql,-1,out,0) : SQLITE_NOMEM;
  sqlite3_free(q); sqlite3_free(sql); return rc;
}
static int rgExec(RgTable *t,char *sql){
  int rc=sql ? sqlite3_exec(t->db,sql,0,0,0) : SQLITE_NOMEM;
  sqlite3_free(sql); return rc;
}
static int rgStatement(RgTable *t,int i,sqlite3_stmt **out){
  static const char *objects[RG_STMTS]={
    "blocks","patch","patch","patch","blocks","bases","bases","values","values","refs"
  };
  static const char *formats[RG_STMTS]={
    "SELECT node,anchor FROM %s WHERE block=?",
    "SELECT state,bits FROM %s WHERE block=? AND slot=?",
    "INSERT INTO %s VALUES(?,?,?,?) ON CONFLICT(block,slot) DO UPDATE SET state=excluded.state,bits=excluded.bits",
    "DELETE FROM %s WHERE block=? AND slot=?",
    "INSERT INTO %s VALUES(?,NULL,0,?,?,?) ON CONFLICT(block) DO NOTHING",
    "SELECT id,data FROM %s WHERE hash=?",
    "INSERT INTO %s(hash,data) VALUES(?,?)",
    "SELECT id,data FROM %s WHERE hash=?",
    "INSERT INTO %s(hash,data) VALUES(?,?)",
    "INSERT OR IGNORE INTO %s VALUES(?,?)"
  };
  int rc=SQLITE_OK;
  if( !t->write[i] ) rc=rgPrepare(t,&t->write[i],objects[i],formats[i]);
  *out=t->write[i]; return rc;
}
static void rgFinish(RgTable *t){
  int i; for(i=0;i<RG_STMTS;i++){ sqlite3_finalize(t->write[i]); t->write[i]=0; }
  t->cacheId=0;
}
static RgWord rgHash(const void *data,int n){
  const RgByte *p=(const RgByte*)data;
  RgWord h=UINT64_C(1469598103934665603);
  int i; for(i=0;i<n;i++){ h^=p[i]; h*=UINT64_C(1099511628211); }
  return h;
}
static int rgIntern(RgTable *t,int object,const void *data,int n,RgInt *id,int *created){
  sqlite3_stmt *find=0,*put=0;
  RgWord hash=rgHash(data,n);
  int rc=rgStatement(t,object ? 7 : 5,&find);
  *created=0;
  if( rc ) return rc;
  sqlite3_bind_int64(find,1,rgSigned(hash));
  while( (rc=sqlite3_step(find))==SQLITE_ROW ){
    if( sqlite3_column_bytes(find,1)==n && !memcmp(sqlite3_column_blob(find,1),data,(size_t)n) ){
      *id=sqlite3_column_int64(find,0); rgReset(find); return SQLITE_OK;
    }
  }
  rgReset(find);
  if( rc!=SQLITE_DONE ) return rc;
  rc=rgStatement(t,object ? 8 : 6,&put);
  if( rc ) return rc;
  sqlite3_bind_int64(put,1,rgSigned(hash));
  rc=sqlite3_bind_blob(put,2,data,n,SQLITE_TRANSIENT);
  if( !rc ) rc=sqlite3_step(put);
  if( rc==SQLITE_DONE ){ *id=sqlite3_last_insert_rowid(t->db); *created=1; rc=SQLITE_OK; }
  rgReset(put); return rc;
}
static void rgTransform(RgWord *w,int axes,int inverse){
  int a,i,step;
  for(a=0;a<3;a++) if( axes&(1<<a) ){
    step=1<<(3*a);
    if( inverse ){
      for(i=0;i<RG_CELLS;i++) if( (i/step)%8 ) w[i]+=w[i-step];
    }else{
      for(i=RG_CELLS-1;i>=0;i--) if( (i/step)%8 ) w[i]-=w[i-step];
    }
  }
}
static int rgEncode(RgTable *t,const RgLeaf *leaf,RgWord *anchor,void **out,int *bytes){
  RgByte raw[RG_RAW];
  RgWord words[RG_CELLS];
  void *best=0;
  int bestSize=0,axes,mode,i,rc=SQLITE_OK;
  *out=0; *bytes=0; *anchor=0;
  if( t->kind<=2 ) for(i=0;i<RG_CELLS;i++){
    if( rgBit(leaf->live,i) && !rgBit(leaf->nulls,i) ){ *anchor=leaf->words[i]; break; }
  }
  for(axes=0;axes<(t->kind<=2 ? 8 : 1);axes++){
    memset(raw,0,RG_HEAD); memcpy(raw,"RSC1",4);
    raw[4]=1; raw[5]=(RgByte)t->kind; raw[7]=(RgByte)axes; raw[9]=2;
    memcpy(raw+RG_HEAD,leaf->live,RG_MAP); memcpy(raw+RG_HEAD+RG_MAP,leaf->nulls,RG_MAP);
    for(i=0;i<RG_CELLS;i++) words[i]=rgBit(leaf->live,i) && !rgBit(leaf->nulls,i)
        ? leaf->words[i]-*anchor : 0;
    rgTransform(words,axes,0);
    for(i=0;i<RG_CELLS;i++) rgPut(raw+RG_HEAD+2*RG_MAP+8*i,words[i]);
    for(mode=0;mode<(t->kind<=2 ? 2 : 1);mode++){
      void *candidate=0,*folded=0;
      int n=0,foldSize=0;
      raw[6]=(RgByte)mode;
      if( mode ){
        RgByte *frame;
        rc=sqlnew_fold_pack(raw+RG_HEAD+2*RG_MAP,8*RG_CELLS,0,&folded,&foldSize);
        if( rc ) goto done;
        frame=(RgByte*)sqlite3_malloc64((RgWord)(RG_HEAD+2*RG_MAP)+foldSize);
        if( !frame ){ sqlite3_free(folded); rc=SQLITE_NOMEM; goto done; }
        memcpy(frame,raw,RG_HEAD+2*RG_MAP);
        memcpy(frame+RG_HEAD+2*RG_MAP,folded,(size_t)foldSize);
        sqlite3_free(folded);
        rc=sqlnew_pack_buffer(t->db,frame,RG_HEAD+2*RG_MAP+foldSize,&candidate,&n);
        sqlite3_free(frame);
      }else rc=sqlnew_pack_buffer(t->db,raw,RG_RAW,&candidate,&n);
      if( rc ) goto done;
      if( !best || n<bestSize ){ sqlite3_free(best); best=candidate; bestSize=n; }
      else sqlite3_free(candidate);
    }
  }
done:
  if( rc ) sqlite3_free(best);
  else { *out=best; *bytes=bestSize; }
  return rc;
}
static int rgDecode(RgTable *t,const void *data,int n,RgLeaf *leaf){
  void *memory=0,*folded=0;
  RgByte *p;
  int bytes=0,foldSize=0,i,rc=sqlnew_unpack_buffer(t->db,data,n,&memory,&bytes);
  if( rc ) return rc;
  p=(RgByte*)memory;
  if( bytes<RG_HEAD+2*RG_MAP || memcmp(p,"RSC1",4) || p[4]!=1 || p[5]!=t->kind
      || p[6]>1 || p[7]>7 || p[8] || p[9]!=2 || p[10] || p[11]
      || p[12] || p[13] || p[14] || p[15] ) { rc=SQLITE_CORRUPT_VTAB; goto done; }
  memcpy(leaf->live,p+RG_HEAD,RG_MAP); memcpy(leaf->nulls,p+RG_HEAD+RG_MAP,RG_MAP);
  if( p[6] ){
    rc=sqlnew_fold_unpack(p+RG_HEAD+2*RG_MAP,bytes-RG_HEAD-2*RG_MAP,&folded,&foldSize);
    if( rc ) goto done;
    if( foldSize!=8*RG_CELLS ){ rc=SQLITE_CORRUPT_VTAB; goto done; }
  }else if( bytes!=RG_RAW ){ rc=SQLITE_CORRUPT_VTAB; goto done; }
  for(i=0;i<RG_CELLS;i++) leaf->words[i]=rgGet((p[6] ? (RgByte*)folded : p+RG_HEAD+2*RG_MAP)+8*i);
  rgTransform(leaf->words,p[7],1);
  t->leavesDecoded++; t->bytesRead+=(RgWord)n;
done:
  sqlite3_free(memory); sqlite3_free(folded); return rc;
}
static int rgLoad(RgTable *t,sqlite3_stmt *node,RgInt id,RgLeaf *leaf){
  int rc;
  memset(leaf,0,sizeof(*leaf));
  if( !id ) return SQLITE_OK;
  sqlite3_bind_int64(node,1,id); rc=sqlite3_step(node);
  if( rc==SQLITE_ROW ) rc=rgDecode(t,sqlite3_column_blob(node,0),sqlite3_column_bytes(node,0),leaf);
  else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  rgReset(node); return rc;
}
static RgInt rgBlock(RgTable *t,const RgInt *c){ return c[0]/8+t->groups[0]*(c[1]/8+t->groups[1]*(c[2]/8)); }
static int rgSlot(const RgInt *c){ return (int)(c[0]%8+8*(c[1]%8)+64*(c[2]%8)); }
static void rgOrigin(RgTable *t,RgInt b,RgInt *o){
  o[0]=(b%t->groups[0])*8; b/=t->groups[0];
  o[1]=(b%t->groups[1])*8; o[2]=(b/t->groups[1])*8;
}
static RgInt rgRowid(RgTable *t,const RgInt *c){ return c[0]+t->n[0]*(c[1]+t->n[1]*c[2]); }
static int rgFromRowid(RgTable *t,RgInt id,RgInt *c){
  if( id<0 || id>=t->total ) return SQLITE_CONSTRAINT;
  c[0]=id%t->n[0]; id/=t->n[0]; c[1]=id%t->n[1]; c[2]=id/t->n[1]; return SQLITE_OK;
}
static int rgBaseCell(RgTable *t,RgInt block,int slot,int *state,RgWord *bits){
  sqlite3_stmt *s=0,*node=0;
  RgInt id=0;
  RgWord anchor=0;
  int rc=rgStatement(t,0,&s);
  *state=0; *bits=0;
  if( rc ) return rc;
  sqlite3_bind_int64(s,1,block); rc=sqlite3_step(s);
  if( rc==SQLITE_ROW ){ id=sqlite3_column_int64(s,0); anchor=(RgWord)sqlite3_column_int64(s,1); rc=SQLITE_OK; }
  else if( rc==SQLITE_DONE ) rc=SQLITE_OK;
  rgReset(s);
  if( rc || !id ) return rc;
  if( t->cacheId!=id ){
    rc=rgPrepare(t,&node,"bases","SELECT data FROM %s WHERE id=?");
    if( !rc ) rc=rgLoad(t,node,id,&t->cache);
    sqlite3_finalize(node);
    if( rc ) return rc;
    t->cacheId=id;
  }
  if( rgBit(t->cache.live,slot) ){
    *state=rgBit(t->cache.nulls,slot) ? 1 : 2;
    *bits=t->cache.words[slot]+anchor;
  }
  return SQLITE_OK;
}
static int rgCurrent(RgTable *t,RgInt block,int slot,int *state,RgWord *bits){
  sqlite3_stmt *s=0;
  int rc=rgStatement(t,1,&s);
  if( rc ) return rc;
  sqlite3_bind_int64(s,1,block); sqlite3_bind_int(s,2,slot); rc=sqlite3_step(s);
  if( rc==SQLITE_ROW ){
    *state=sqlite3_column_int(s,0); *bits=(RgWord)sqlite3_column_int64(s,1); rc=SQLITE_OK;
    rgReset(s); return rc;
  }
  rgReset(s);
  return rc==SQLITE_DONE ? rgBaseCell(t,block,slot,state,bits) : rc;
}
static int rgStore(RgTable *t,const RgInt *c,int state,RgWord bits){
  sqlite3_stmt *s=0;
  RgInt block=rgBlock(t,c);
  RgWord original=0;
  int slot=rgSlot(c),old=0,rc=rgBaseCell(t,block,slot,&old,&original),same;
  if( rc ) return rc;
  same=old==state && (state!=2 || original==bits);
  if( !same ){
    rc=rgStatement(t,4,&s);
    if( rc ) return rc;
    sqlite3_bind_int64(s,1,block);
    sqlite3_bind_int64(s,2,c[0]/8); sqlite3_bind_int64(s,3,c[1]/8); sqlite3_bind_int64(s,4,c[2]/8);
    rc=sqlite3_step(s); rgReset(s);
    if( rc!=SQLITE_DONE ) return rc;
  }
  rc=rgStatement(t,same ? 3 : 2,&s);
  if( rc ) return rc;
  sqlite3_bind_int64(s,1,block); sqlite3_bind_int(s,2,slot);
  if( !same ){ sqlite3_bind_int(s,3,state); sqlite3_bind_int64(s,4,rgSigned(bits)); }
  rc=sqlite3_step(s); rgReset(s);
  if( rc==SQLITE_DONE ){ t->patchesWritten++; rc=SQLITE_OK; }
  return rc;
}
static int rgValue(RgTable *t,sqlite3_value *value,int *state,RgWord *bits){
  int type=sqlite3_value_type(value),rc,created;
  *state=type==SQLITE_NULL ? 1 : 2; *bits=0;
  if( type==SQLITE_NULL ) return SQLITE_OK;
  if( t->kind==1 ){
    if( type!=SQLITE_INTEGER ) return SQLITE_MISMATCH;
    *bits=(RgWord)sqlite3_value_int64(value);
  }else if( t->kind==2 ){
    double real;
    if( type!=SQLITE_INTEGER && type!=SQLITE_FLOAT ) return SQLITE_MISMATCH;
    real=sqlite3_value_double(value);
    if( !(real<=DBL_MAX && real>=-DBL_MAX) ) return SQLITE_CONSTRAINT;
    memcpy(bits,&real,8);
  }else{
    const void *data;
    void *packed=0;
    RgInt id=0;
    int bytes=0;
    if( type!=(t->kind==3 ? SQLITE_TEXT : SQLITE_BLOB) ) return SQLITE_MISMATCH;
    data=t->kind==3 ? (const void*)sqlite3_value_text(value) : sqlite3_value_blob(value);
    rc=sqlnew_pack_buffer(t->db,data,sqlite3_value_bytes(value),&packed,&bytes);
    if( !rc ) rc=rgIntern(t,1,packed,bytes,&id,&created);
    sqlite3_free(packed);
    if( rc ) return rc;
    *bits=(RgWord)id;
  }
  return SQLITE_OK;
}
static int rgResult(RgTable *t,sqlite3_stmt *object,sqlite3_context *ctx,int state,RgWord bits){
  if( state<2 ){ sqlite3_result_null(ctx); return SQLITE_OK; }
  if( t->kind==1 ) sqlite3_result_int64(ctx,rgSigned(bits));
  else if( t->kind==2 ){
    double real; memcpy(&real,&bits,8); sqlite3_result_double(ctx,real);
  }else{
    void *data=0;
    int n=0,rc;
    sqlite3_bind_int64(object,1,(RgInt)bits); rc=sqlite3_step(object);
    if( rc==SQLITE_ROW ){
      t->objectsRead++; t->bytesRead+=(RgWord)sqlite3_column_bytes(object,0);
      rc=sqlnew_unpack_buffer(t->db,sqlite3_column_blob(object,0),sqlite3_column_bytes(object,0),&data,&n);
    }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
    rgReset(object);
    if( rc ) return rc;
    if( t->kind==3 ) sqlite3_result_text(ctx,(const char*)data,n,sqlite3_free);
    else sqlite3_result_blob(ctx,data,n,sqlite3_free);
  }
  return SQLITE_OK;
}
static int rgOpen(sqlite3_vtab *v,sqlite3_vtab_cursor **out){
  RgCursor *c=(RgCursor*)sqlite3_malloc64(sizeof(*c));
  if( !c ) return SQLITE_NOMEM;
  memset(c,0,sizeof(*c)); c->base.pVtab=v; *out=&c->base; return SQLITE_OK;
}
static int rgClose(sqlite3_vtab_cursor *v){
  RgCursor *c=(RgCursor*)v;
  sqlite3_finalize(c->blocks); sqlite3_finalize(c->patch); sqlite3_finalize(c->node); sqlite3_finalize(c->object);
  sqlite3_free(c); return SQLITE_OK;
}
static int rgBest(sqlite3_vtab *v,sqlite3_index_info *p){
  char *plan=(char*)sqlite3_malloc64((RgWord)p->nConstraint*2+1);
  int i,n=0,equalities=0;
  (void)v;
  if( !plan ) return SQLITE_NOMEM;
  for(i=0;i<p->nConstraint;i++){
    int col=p->aConstraint[i].iColumn,op=p->aConstraint[i].op;
    if( !p->aConstraint[i].usable || col>2 || col< -1 ) continue;
    if( op!=SQLITE_INDEX_CONSTRAINT_EQ && op!=SQLITE_INDEX_CONSTRAINT_GE
        && op!=SQLITE_INDEX_CONSTRAINT_GT && op!=SQLITE_INDEX_CONSTRAINT_LE && op!=SQLITE_INDEX_CONSTRAINT_LT ) continue;
    plan[n*2]=(char)('0'+col+1); plan[n*2+1]=(char)op;
    p->aConstraintUsage[i].argvIndex=++n;
    if( op==SQLITE_INDEX_CONSTRAINT_EQ ) equalities|=1<<(col+1);
  }
  plan[n*2]=0; p->idxStr=plan; p->needToFreeIdxStr=1;
  if( equalities&1 || (equalities&14)==14 ){
    p->idxFlags=SQLITE_INDEX_SCAN_UNIQUE; p->estimatedRows=1; p->estimatedCost=4;
  }else{ p->estimatedRows=equalities ? 4096 : 1000000; p->estimatedCost=(double)p->estimatedRows; }
  return SQLITE_OK;
}
static int rgSelected(RgCursor *c,RgInt *coordinate){
  RgTable *t=(RgTable*)c->base.pVtab;
  int i;
  coordinate[0]=c->origin[0]+c->slot%8;
  coordinate[1]=c->origin[1]+(c->slot/8)%8; coordinate[2]=c->origin[2]+c->slot/64;
  for(i=0;i<3;i++) if( coordinate[i]<c->lo[i] || coordinate[i]>c->hi[i] || coordinate[i]>=t->n[i] ) return 0;
  c->rowid=rgRowid(t,coordinate);
  return c->rowid>=c->rowLo && c->rowid<=c->rowHi;
}
static int rgNext(sqlite3_vtab_cursor *v){
  RgCursor *c=(RgCursor*)v;
  RgTable *t=(RgTable*)v->pVtab;
  RgInt coordinate[3];
  int rc;
  for(;;){
    while( ++c->slot<RG_CELLS ){
      int state;
      if( !rgSelected(c,coordinate) ) continue;
      if( c->patched[c->slot] ) state=c->patchState[c->slot];
      else{
        if( !c->loaded ){
          if( c->decodedId!=c->nodeId ){
            if( c->nodeId && t->cacheId==c->nodeId ) memcpy(&c->leaf,&t->cache,sizeof(c->leaf));
            else{
              rc=rgLoad(t,c->node,c->nodeId,&c->leaf);
              if( rc ) return rc;
              if( c->nodeId ){
                memcpy(&t->cache,&c->leaf,sizeof(t->cache)); t->cacheId=c->nodeId;
              }
            }
            c->decodedId=c->nodeId;
          }
          c->loaded=1;
        }
        state=rgBit(c->leaf.live,c->slot) ? 1 : 0;
      }
      if( state ){ t->rowsOut++; return SQLITE_OK; }
    }
    rc=sqlite3_step(c->blocks);
    if( rc==SQLITE_DONE ){ c->eof=1; return SQLITE_OK; }
    if( rc!=SQLITE_ROW ) return rc;
    c->block=sqlite3_column_int64(c->blocks,0); c->nodeId=sqlite3_column_int64(c->blocks,1);
    c->anchor=(RgWord)sqlite3_column_int64(c->blocks,2); rgOrigin(t,c->block,c->origin);
    c->loaded=0; memset(c->patched,0,sizeof(c->patched)); t->blocksRead++;
    sqlite3_bind_int64(c->patch,1,c->block);
    if( c->point ){
      RgInt point[3]; point[0]=c->lo[0]; point[1]=c->lo[1]; point[2]=c->lo[2];
      sqlite3_bind_int(c->patch,2,rgSlot(point));
    }else{
      int low[3],high[3],a;
      for(a=0;a<3;a++){
        low[a]=(int)(c->lo[a]>c->origin[a] ? c->lo[a]-c->origin[a] : 0);
        high[a]=(int)(c->hi[a]<c->origin[a]+7 ? c->hi[a]-c->origin[a] : 7);
      }
      sqlite3_bind_int(c->patch,2,low[2]*64+low[1]*8+low[0]);
      sqlite3_bind_int(c->patch,3,high[2]*64+high[1]*8+high[0]);
      sqlite3_bind_int(c->patch,4,low[0]); sqlite3_bind_int(c->patch,5,high[0]);
      sqlite3_bind_int(c->patch,6,low[1]); sqlite3_bind_int(c->patch,7,high[1]);
    }
    while( (rc=sqlite3_step(c->patch))==SQLITE_ROW ){
      int slot=sqlite3_column_int(c->patch,0),state=sqlite3_column_int(c->patch,1);
      if( slot<0 || slot>=RG_CELLS || state<0 || state>2 ){ rgReset(c->patch); return SQLITE_CORRUPT_VTAB; }
      c->patched[slot]=1; c->patchState[slot]=(RgByte)state;
      c->patchBits[slot]=(RgWord)sqlite3_column_int64(c->patch,2); t->patchesRead++;
    }
    rgReset(c->patch);
    if( rc!=SQLITE_DONE ) return rc;
    c->slot=-1;
    if( sqlite3_is_interrupted(t->db) ) return SQLITE_INTERRUPT;
  }
}
static int rgFilter(sqlite3_vtab_cursor *v,int index,const char *plan,int argc,sqlite3_value **argv){
  RgCursor *c=(RgCursor*)v;
  RgTable *t=(RgTable*)v->pVtab;
  int i,rc,empty=0;
  (void)index;
  sqlite3_finalize(c->blocks); c->blocks=0;
  sqlite3_finalize(c->patch); c->patch=0;
  c->rowLo=0; c->rowHi=t->total-1;
  for(i=0;i<3;i++){ c->lo[i]=0; c->hi[i]=t->n[i]-1; }
  for(i=0;i<argc;i++){
    int col=plan[i*2]-'0'-1,op=(unsigned char)plan[i*2+1],kind=sqlite3_value_numeric_type(argv[i]);
    RgInt value,*lo=col<0 ? &c->rowLo : &c->lo[col],*hi=col<0 ? &c->rowHi : &c->hi[col];
    if( kind==SQLITE_NULL ){ empty=1; continue; }
    if( kind!=SQLITE_INTEGER && kind!=SQLITE_FLOAT ) continue;
    if( kind==SQLITE_INTEGER ) value=sqlite3_value_int64(argv[i]);
    else{
      double real=sqlite3_value_double(argv[i]);
      value=real>=(double)INT64_MAX ? INT64_MAX : real<=(double)INT64_MIN ? INT64_MIN : (RgInt)real;
    }
    if( op==SQLITE_INDEX_CONSTRAINT_EQ ){ if( value>*lo ) *lo=value; if( value<*hi ) *hi=value; }
    else if( op==SQLITE_INDEX_CONSTRAINT_GT || op==SQLITE_INDEX_CONSTRAINT_GE ){ if( value>*lo ) *lo=value; }
    else if( value<*hi ) *hi=value;
  }
  if( c->rowLo==c->rowHi ){
    RgInt point[3];
    if( rgFromRowid(t,c->rowLo,point) ) empty=1;
    else for(i=0;i<3;i++){
      if( point[i]<c->lo[i] || point[i]>c->hi[i] ) empty=1;
      c->lo[i]=c->hi[i]=point[i];
    }
  }
  for(i=0;i<3;i++) if( c->lo[i]>c->hi[i] ) empty=1;
  if( c->rowLo>c->rowHi ) empty=1;
  c->eof=empty; c->slot=RG_CELLS; c->point=1; c->decodedId=-1;
  if( empty ) return SQLITE_OK;
  for(i=0;i<3;i++) if( c->lo[i]!=c->hi[i] ) c->point=0;
  if( c->point ){
    rc=rgPrepare(t,&c->blocks,"blocks","SELECT block,node,anchor FROM %s WHERE block=?");
    if( !rc ) sqlite3_bind_int64(c->blocks,1,rgBlock(t,c->lo));
  }else{
    rc=rgPrepare(t,&c->blocks,"blocks","SELECT block,node,anchor FROM %s WHERE bx BETWEEN ?1 AND ?2 AND by BETWEEN ?3 AND ?4 AND bz BETWEEN ?5 AND ?6 ORDER BY block");
    if( !rc ) for(i=0;i<3;i++){
      sqlite3_bind_int64(c->blocks,2*i+1,c->lo[i]/8); sqlite3_bind_int64(c->blocks,2*i+2,c->hi[i]/8);
    }
  }
  if( !rc ) rc=rgPrepare(t,&c->patch,"patch",c->point
      ? "SELECT slot,state,bits FROM %s WHERE block=?1 AND slot=?2"
      : "SELECT slot,state,bits FROM %s WHERE block=?1 AND slot BETWEEN ?2 AND ?3 AND slot%%8 BETWEEN ?4 AND ?5 AND (slot/8)%%8 BETWEEN ?6 AND ?7 ORDER BY slot");
  if( !rc && !c->node ) rc=rgPrepare(t,&c->node,"bases","SELECT data FROM %s WHERE id=?");
  if( !rc && t->kind>=3 && !c->object ) rc=rgPrepare(t,&c->object,"values","SELECT data FROM %s WHERE id=?");
  return rc ? rc : rgNext(v);
}
static int rgEof(sqlite3_vtab_cursor *v){ return ((RgCursor*)v)->eof; }
static int rgColumn(sqlite3_vtab_cursor *v,sqlite3_context *ctx,int col){
  RgCursor *c=(RgCursor*)v;
  RgTable *t=(RgTable*)v->pVtab;
  RgInt coordinate[3];
  int state;
  RgWord bits;
  if( sqlite3_vtab_nochange(ctx) ) return SQLITE_OK;
  if( col<3 ){
    rgSelected(c,coordinate); sqlite3_result_int64(ctx,coordinate[col]); return SQLITE_OK;
  }
  if( col==4 ) return SQLITE_OK;
  if( c->patched[c->slot] ){ state=c->patchState[c->slot]; bits=c->patchBits[c->slot]; }
  else { state=rgBit(c->leaf.nulls,c->slot) ? 1 : 2; bits=c->leaf.words[c->slot]+c->anchor; }
  return rgResult(t,c->object,ctx,state,bits);
}
static int rgRow(sqlite3_vtab_cursor *v,RgInt *id){ *id=((RgCursor*)v)->rowid; return SQLITE_OK; }
static int rgCompact(RgTable *t){
  sqlite3_stmt *blocks=0,*patch=0,*node=0,*publish=0,*clear=0,*remove=0;
  int rc=rgPrepare(t,&blocks,"blocks","SELECT block,node,anchor FROM %s ORDER BY block");
  if( !rc ) rc=rgPrepare(t,&patch,"patch","SELECT slot,state,bits FROM %s WHERE block=? ORDER BY slot");
  if( !rc ) rc=rgPrepare(t,&node,"bases","SELECT data FROM %s WHERE id=?");
  if( !rc ) rc=rgPrepare(t,&publish,"blocks","UPDATE %s SET node=?,anchor=? WHERE block=?");
  if( !rc ) rc=rgPrepare(t,&clear,"patch","DELETE FROM %s WHERE block=?");
  if( !rc ) rc=rgPrepare(t,&remove,"blocks","DELETE FROM %s WHERE block=?");
  t->cacheId=0;
  while( !rc && (rc=sqlite3_step(blocks))==SQLITE_ROW ){
    RgLeaf leaf;
    RgInt block=sqlite3_column_int64(blocks,0),oldId=sqlite3_column_int64(blocks,1),id=0;
    RgWord oldAnchor=(RgWord)sqlite3_column_int64(blocks,2),anchor=0;
    void *packed=0;
    int i,n=0,created=0,live=0;
    rc=rgLoad(t,node,oldId,&leaf);
    if( rc ) break;
    for(i=0;i<RG_CELLS;i++) if( rgBit(leaf.live,i) && !rgBit(leaf.nulls,i) ) leaf.words[i]+=oldAnchor;
    sqlite3_bind_int64(patch,1,block);
    while( !rc && (rc=sqlite3_step(patch))==SQLITE_ROW ){
      int slot=sqlite3_column_int(patch,0),state=sqlite3_column_int(patch,1);
      if( slot<0 || slot>=RG_CELLS || state<0 || state>2 ){ rc=SQLITE_CORRUPT_VTAB; break; }
      leaf.live[slot>>3]&=(RgByte)~(1u<<(slot&7)); leaf.nulls[slot>>3]&=(RgByte)~(1u<<(slot&7));
      if( state ) rgSet(leaf.live,slot);
      if( state==1 ) rgSet(leaf.nulls,slot);
      leaf.words[slot]=state==2 ? (RgWord)sqlite3_column_int64(patch,2) : 0;
      rc=SQLITE_OK;
    }
    rgReset(patch);
    if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    if( rc ) break;
    for(i=0;i<RG_CELLS;i++) live+=rgBit(leaf.live,i);
    if( live ){
      rc=rgEncode(t,&leaf,&anchor,&packed,&n);
      if( !rc ) rc=rgIntern(t,0,packed,n,&id,&created);
      sqlite3_free(packed);
      if( rc ) break;
      if( t->kind>=3 && created ) for(i=0;i<RG_CELLS && !rc;i++){
        if( rgBit(leaf.live,i) && !rgBit(leaf.nulls,i) ){
          sqlite3_stmt *refs=0;
          rc=rgStatement(t,9,&refs);
          if( !rc ){
            sqlite3_bind_int64(refs,1,id); sqlite3_bind_int64(refs,2,(RgInt)leaf.words[i]);
            rc=sqlite3_step(refs); rgReset(refs); if( rc==SQLITE_DONE ) rc=SQLITE_OK;
          }
        }
      }
      if( rc ) break;
      sqlite3_bind_int64(publish,1,id); sqlite3_bind_int64(publish,2,rgSigned(anchor));
      sqlite3_bind_int64(publish,3,block); rc=sqlite3_step(publish); rgReset(publish);
      if( rc!=SQLITE_DONE ) break;
      if( created ) t->baseWrites++; else t->baseReuse++;
    }else{
      sqlite3_bind_int64(remove,1,block); rc=sqlite3_step(remove); rgReset(remove);
      if( rc!=SQLITE_DONE ) break;
    }
    sqlite3_bind_int64(clear,1,block); rc=sqlite3_step(clear); rgReset(clear);
    if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    if( !rc && sqlite3_is_interrupted(t->db) ) rc=SQLITE_INTERRUPT;
  }
  if( rc==SQLITE_DONE ) rc=SQLITE_OK;
  sqlite3_finalize(blocks); sqlite3_finalize(patch); sqlite3_finalize(node);
  sqlite3_finalize(publish); sqlite3_finalize(clear); sqlite3_finalize(remove);
  if( !rc ) rc=rgExec(t,sqlite3_mprintf(
    "DELETE FROM \"%w\".\"%w_refs\" WHERE base NOT IN(SELECT node FROM \"%w\".\"%w_blocks\" WHERE node IS NOT NULL);"
    "DELETE FROM \"%w\".\"%w_bases\" WHERE id NOT IN(SELECT node FROM \"%w\".\"%w_blocks\" WHERE node IS NOT NULL);"
    "DELETE FROM \"%w\".\"%w_values\" WHERE id NOT IN(SELECT value FROM \"%w\".\"%w_refs\") AND id NOT IN(SELECT bits FROM \"%w\".\"%w_patch\" WHERE state=2)",
    t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,
    t->schema,t->name,t->schema,t->name,t->schema,t->name));
  if( !rc ) t->compactions++;
  t->cacheId=0; return rc;
}
static int rgUpdate(sqlite3_vtab *v,int argc,sqlite3_value **argv,RgInt *id){
  RgTable *t=(RgTable*)v;
  RgInt coordinate[3],oldCoordinate[3],target=0,saved=sqlite3_last_insert_rowid(t->db);
  RgWord bits=0,current=0;
  int i,rc=SQLITE_OK,state=0,exists=0,insert;
  if( t->busy ) return SQLITE_LOCKED;
  t->busy=1;
  if( argc==1 ){
    rc=rgFromRowid(t,sqlite3_value_int64(argv[0]),coordinate);
    if( !rc ) rc=rgStore(t,coordinate,0,0);
    goto done;
  }
  if( sqlite3_value_type(argv[6])!=SQLITE_NULL ){
    const char *command=(const char*)sqlite3_value_text(argv[6]);
    rc=command && !sqlite3_stricmp(command,"compact") ? rgCompact(t) : SQLITE_ERROR;
    *id=0; goto done;
  }
  insert=sqlite3_value_type(argv[0])==SQLITE_NULL;
  if( !insert ){
    rc=rgFromRowid(t,sqlite3_value_int64(argv[0]),oldCoordinate);
    if( rc ) goto done;
  }
  for(i=0;i<3;i++){
    if( !insert && sqlite3_value_nochange(argv[2+i]) ) coordinate[i]=oldCoordinate[i];
    else{
      if( sqlite3_value_type(argv[2+i])!=SQLITE_INTEGER ){ rc=SQLITE_MISMATCH; goto done; }
      coordinate[i]=sqlite3_value_int64(argv[2+i]);
    }
    if( coordinate[i]<0 || coordinate[i]>=t->n[i] ){ rc=SQLITE_CONSTRAINT; goto done; }
  }
  target=rgRowid(t,coordinate);
  if( sqlite3_value_type(argv[1])!=SQLITE_NULL && sqlite3_value_int64(argv[1])!=target
      && (insert || sqlite3_value_int64(argv[1])!=sqlite3_value_int64(argv[0])) ){
    rc=SQLITE_CONSTRAINT; goto done;
  }
  rc=rgCurrent(t,rgBlock(t,coordinate),rgSlot(coordinate),&exists,&current);
  if( rc ) goto done;
  if( exists && (insert || target!=sqlite3_value_int64(argv[0])) ){
    int policy=sqlite3_vtab_on_conflict(t->db);
    if( policy==SQLITE_IGNORE ){ *id=target; goto done; }
    if( policy!=SQLITE_REPLACE ){ rc=SQLITE_CONSTRAINT; goto done; }
  }
  if( !insert && sqlite3_value_nochange(argv[5]) ){
    rc=rgCurrent(t,rgBlock(t,oldCoordinate),rgSlot(oldCoordinate),&state,&bits);
  }else rc=rgValue(t,argv[5],&state,&bits);
  if( !rc && !insert && target!=sqlite3_value_int64(argv[0]) ) rc=rgStore(t,oldCoordinate,0,0);
  if( !rc ) rc=rgStore(t,coordinate,state,bits);
  *id=target;
done:
  sqlite3_set_last_insert_rowid(t->db,saved); t->busy=0; return rc;
}
static int rgBegin(sqlite3_vtab *v){ ((RgTable*)v)->cacheId=0; return SQLITE_OK; }
static int rgSync(sqlite3_vtab *v){ ((RgTable*)v)->cacheId=0; return SQLITE_OK; }
static int rgEnd(sqlite3_vtab *v){ rgFinish((RgTable*)v); return SQLITE_OK; }
static int rgSave(sqlite3_vtab *v,int n){ (void)n; ((RgTable*)v)->cacheId=0; return SQLITE_OK; }
static int rgDisconnect(sqlite3_vtab *v){
  RgTable *t=(RgTable*)v,**link=&t->registry->tables;
  while( *link && *link!=t ) link=&(*link)->next;
  if( *link ) *link=t->next;
  rgFinish(t); rgRelease(t->registry);
  sqlite3_free(t->schema); sqlite3_free(t->name); sqlite3_free(t); return SQLITE_OK;
}
static int rgConnect(sqlite3 *db,void *aux,int argc,const char *const*argv,sqlite3_vtab **out,char **error){
  RgTable *t;
  int i,rc;
  char *decl;
  if( argc!=7 ){ *error=sqlite3_mprintf("sqlnew_region requires nx, ny, nz, INTEGER|REAL|TEXT|BLOB"); return SQLITE_ERROR; }
  t=(RgTable*)sqlite3_malloc64(sizeof(*t));
  if( !t ) return SQLITE_NOMEM;
  memset(t,0,sizeof(*t)); t->db=db; t->registry=(RgRegistry*)aux; t->registry->refs++;
  t->schema=sqlite3_mprintf("%s",argv[1]); t->name=sqlite3_mprintf("%s",argv[2]); t->total=1;
  if( !t->schema || !t->name ){ rgDisconnect(&t->base); return SQLITE_NOMEM; }
  for(i=0;i<3;i++){
    char *end=0;
    unsigned long long n;
    errno=0; n=strtoull(argv[3+i],&end,10);
    if( errno || !end || *end || n==0 || n>INT32_MAX || t->total>INT64_MAX/(RgInt)n ){
      *error=sqlite3_mprintf("region dimensions must be positive and their product fit signed int64"); rgDisconnect(&t->base); return SQLITE_ERROR;
    }
    t->n[i]=(RgInt)n; t->groups[i]=((RgInt)n+7)/8; t->total*=t->n[i];
  }
  if( !sqlite3_stricmp(argv[6],"INTEGER") ) t->kind=1;
  else if( !sqlite3_stricmp(argv[6],"REAL") ) t->kind=2;
  else if( !sqlite3_stricmp(argv[6],"TEXT") ) t->kind=3;
  else if( !sqlite3_stricmp(argv[6],"BLOB") ) t->kind=4;
  else { *error=sqlite3_mprintf("unsupported region value type"); rgDisconnect(&t->base); return SQLITE_ERROR; }
  decl=sqlite3_mprintf("CREATE TABLE x(x INTEGER,y INTEGER,z INTEGER,value %s,__sqlnew_command TEXT HIDDEN)",argv[6]);
  rc=decl ? sqlite3_declare_vtab(db,decl) : SQLITE_NOMEM; sqlite3_free(decl);
  if( rc ){ rgDisconnect(&t->base); return rc; }
  sqlite3_vtab_config(db,SQLITE_VTAB_CONSTRAINT_SUPPORT,1);
  t->next=t->registry->tables; t->registry->tables=t;
  *out=&t->base; return SQLITE_OK;
}
static int rgCreate(sqlite3 *db,void *aux,int argc,const char *const*argv,sqlite3_vtab **out,char **error){
  RgTable *t;
  int rc=rgConnect(db,aux,argc,argv,out,error);
  if( rc ) return rc;
  t=(RgTable*)*out;
  rc=rgExec(t,sqlite3_mprintf(
    "CREATE TABLE \"%w\".\"%w_blocks\"(block INTEGER PRIMARY KEY,node INTEGER,anchor INTEGER NOT NULL,bx INTEGER NOT NULL,by INTEGER NOT NULL,bz INTEGER NOT NULL);"
    "CREATE TABLE \"%w\".\"%w_bases\"(id INTEGER PRIMARY KEY AUTOINCREMENT,hash INTEGER NOT NULL,data BLOB NOT NULL,UNIQUE(hash,id));"
    "CREATE TABLE \"%w\".\"%w_values\"(id INTEGER PRIMARY KEY AUTOINCREMENT,hash INTEGER NOT NULL,data BLOB NOT NULL,UNIQUE(hash,id));"
    "CREATE TABLE \"%w\".\"%w_patch\"(block INTEGER,slot INTEGER,state INTEGER NOT NULL,bits INTEGER NOT NULL,PRIMARY KEY(block,slot)) WITHOUT ROWID;"
    "CREATE TABLE \"%w\".\"%w_refs\"(base INTEGER,value INTEGER,PRIMARY KEY(base,value)) WITHOUT ROWID",
    t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name));
  if( rc ){ rgDisconnect(*out); *out=0; }
  return rc;
}
static int rgDestroy(sqlite3_vtab *v){
  RgTable *t=(RgTable*)v;
  int rc;
  rgFinish(t);
  rc=rgExec(t,sqlite3_mprintf("DROP TABLE \"%w\".\"%w_refs\";DROP TABLE \"%w\".\"%w_patch\";DROP TABLE \"%w\".\"%w_blocks\";DROP TABLE \"%w\".\"%w_bases\";DROP TABLE \"%w\".\"%w_values\"",
      t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name));
  return rc ? rc : rgDisconnect(v);
}
static int rgRename(sqlite3_vtab *v,const char *name){
  RgTable *t=(RgTable*)v;
  char *copy=sqlite3_mprintf("%s",name);
  int rc;
  if( !copy ) return SQLITE_NOMEM;
  rgFinish(t);
  rc=rgExec(t,sqlite3_mprintf(
    "ALTER TABLE \"%w\".\"%w_refs\" RENAME TO \"%w_refs\";"
    "ALTER TABLE \"%w\".\"%w_patch\" RENAME TO \"%w_patch\";"
    "ALTER TABLE \"%w\".\"%w_blocks\" RENAME TO \"%w_blocks\";"
    "ALTER TABLE \"%w\".\"%w_bases\" RENAME TO \"%w_bases\";"
    "ALTER TABLE \"%w\".\"%w_values\" RENAME TO \"%w_values\"",
    t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name));
  if( !rc ){ sqlite3_free(t->name); t->name=copy; }else sqlite3_free(copy);
  return rc;
}
static int rgShadow(const char *name){
  return !sqlite3_stricmp(name,"blocks") || !sqlite3_stricmp(name,"bases")
      || !sqlite3_stricmp(name,"values") || !sqlite3_stricmp(name,"patch") || !sqlite3_stricmp(name,"refs");
}
static int rgIntegrity(sqlite3_vtab *v,const char *schema,const char *name,int quick,char **error){
  RgTable *t=(RgTable*)v;
  sqlite3_stmt *s=0;
  int rc=rgPrepare(t,&s,"bases","SELECT data FROM %s");
  (void)schema; (void)name; (void)quick;
  while( !rc && (rc=sqlite3_step(s))==SQLITE_ROW ){
    RgLeaf leaf;
    rc=rgDecode(t,sqlite3_column_blob(s,0),sqlite3_column_bytes(s,0),&leaf);
  }
  if( rc==SQLITE_DONE ) rc=SQLITE_OK;
  sqlite3_finalize(s);
  if( rc ){ *error=sqlite3_mprintf("invalid shared region payload in %s",t->name); return *error ? SQLITE_OK : SQLITE_NOMEM; }
  return SQLITE_OK;
}
static sqlite3_module rgModule={
  4,rgCreate,rgConnect,rgBest,rgDisconnect,rgDestroy,rgOpen,rgClose,rgFilter,rgNext,rgEof,rgColumn,rgRow,
  rgUpdate,rgBegin,rgSync,rgEnd,rgEnd,0,rgRename,rgSave,rgSave,rgSave,rgShadow,rgIntegrity
};
static void rgStatistics(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  RgRegistry *r=(RgRegistry*)sqlite3_user_data(ctx);
  const char *name=(const char*)sqlite3_value_text(argv[0]);
  RgTable *t;
  char *json;
  (void)argc;
  for(t=r->tables;t;t=t->next) if( name ){
    int length=(int)strlen(t->schema);
    if( (!sqlite3_stricmp(t->schema,"main") && !sqlite3_stricmp(t->name,name))
        || (!sqlite3_strnicmp(name,t->schema,length) && name[length]=='.'
            && !sqlite3_stricmp(name+length+1,t->name)) ) break;
  }
  if( !t ){ sqlite3_result_error(ctx,"region table is not connected",-1); return; }
  json=sqlite3_mprintf("{\"blocks_read\":%llu,\"leaves_decoded\":%llu,\"patches_read\":%llu,\"patches_written\":%llu,\"objects_read\":%llu,\"compressed_bytes_read\":%llu,\"rows_emitted\":%llu,\"base_payloads_written\":%llu,\"base_payloads_reused\":%llu,\"compactions\":%llu}",
      t->blocksRead,t->leavesDecoded,t->patchesRead,t->patchesWritten,t->objectsRead,t->bytesRead,t->rowsOut,t->baseWrites,t->baseReuse,t->compactions);
  if( !json ) sqlite3_result_error_nomem(ctx); else sqlite3_result_text(ctx,json,-1,sqlite3_free);
}
static void rgCapabilities(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  (void)argc; (void)argv;
  sqlite3_result_text(ctx,"{\"columnar\":1,\"fold\":1,\"region\":1,\"region_format\":\"RSC1\",\"file_format\":2}",-1,SQLITE_STATIC);
}
int sqlnew_region_init(sqlite3 *db){
  RgRegistry *r=(RgRegistry*)sqlite3_malloc64(sizeof(*r));
  int rc;
  if( !r ) return SQLITE_NOMEM;
  memset(r,0,sizeof(*r)); r->refs=1;
  rc=sqlite3_create_module_v2(db,"sqlnew_region",&rgModule,r,rgRelease);
  if( rc ) return rc;
  r->refs++;
  rc=sqlite3_create_function_v2(db,"sqlnew_region_stats",1,SQLITE_UTF8,r,rgStatistics,0,0,rgRelease);
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_capabilities",0,SQLITE_UTF8,0,rgCapabilities,0,0);
  return rc;
}
