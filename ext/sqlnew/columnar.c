/* Writable column groups with projection, range pruning and SQL summaries. */
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT3
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <float.h>
#include <math.h>

#define CS_ROWS 1024
#define CS_MAP (CS_ROWS/8)
#define CS_COLS 63
#define CS_HEADER 32
#define CS_LIMIT (64*1024*1024-16)
typedef sqlite3_int64 CsInt;
typedef sqlite3_uint64 CsUnsigned;
typedef unsigned char CsByte;
typedef struct CsCell CsCell;
struct CsCell {
  int type,n;
  union { CsInt i; double r; CsByte *p; } v;
};
typedef struct CsWide CsWide;
struct CsWide { CsUnsigned lo; CsInt hi; };
typedef struct CsStats CsStats;
struct CsStats { int count; CsCell *min,*max; CsWide sum,low,high; };
typedef struct CsBlock CsBlock;
struct CsBlock {
  CsInt id;
  CsByte live[CS_MAP];
  int count,dirty,persisted;
  CsUnsigned loaded,changed;
  CsCell *cells[CS_COLS];
};
typedef struct CsRegistry CsRegistry;
typedef struct CsTable CsTable;
typedef struct CsIndex CsIndex;
struct CsIndex {
  CsIndex *next;
  int id,nkey,ncol,active,cols[CS_COLS],desc[CS_COLS];
  CsUnsigned mask;
  char *name;
  sqlite3_stmt *put,*remove,*check;
};
struct CsRegistry {
  CsTable *tables;
  CsUnsigned refs,bytes,peakBytes,byteLimit;
  int folding;
};
/* Immutable TEXT/BLOB payloads are shared by dictionary rows, snapshots and
** aggregate cells. Cell arrays remain private, so replacement never mutates
** another reader's value. All live payloads share a connection byte budget. */
typedef struct CsBytes CsBytes;
struct CsBytes { CsUnsigned refs,size; CsRegistry *registry; };
struct CsTable {
  sqlite3_vtab base;
  sqlite3 *db;
  CsRegistry *registry;
  CsTable *link;
  char *schema,*name;
  char *names[CS_COLS];
  int types[CS_COLS],ncol,inWrite,full,busy,reset,binaryUtf8;
  CsInt next;
  CsBlock *write;
  sqlite3_stmt *patchRead;
  CsIndex *indexes;
  CsUnsigned blocksRead,columnsRead,bytesRead,rowsOut,summaryBlocks,blocksWritten;
  CsUnsigned columnsWritten,patchesWritten,patchesRead,compactions,filteredRows,batchRows;
  CsUnsigned indexReads,indexWrites,batchFallbacks;
};
typedef struct CsCursor CsCursor;
typedef struct CsPredicate CsPredicate;
struct CsPredicate { int col,op; CsCell value; };
struct CsCursor {
  sqlite3_vtab_cursor base;
  sqlite3_stmt *blocks,*column;
  CsBlock *block,*pending;
  int pos,eof,pendingDone,sqlReady;
  CsInt lower,upper;
  int hasLower,hasUpper,strictLower,strictUpper;
  CsPredicate *pred;
  int nPred,alwaysEmpty;
  CsByte selected[CS_MAP];
  sqlite3_stmt *indexScan;
  int indexCols[CS_COLS],indexCount;
};
typedef struct CsBuffer CsBuffer;
struct CsBuffer { CsByte *p; int n,cap,rc; };

static uint32_t csGet32(const CsByte *p){
  return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static void csPut32(CsByte *p,uint32_t v){
  p[0]=(CsByte)v; p[1]=(CsByte)(v>>8); p[2]=(CsByte)(v>>16); p[3]=(CsByte)(v>>24);
}
static CsUnsigned csGet64(const CsByte *p){ return csGet32(p)|((CsUnsigned)csGet32(p+4)<<32); }
static void csPut64(CsByte *p,CsUnsigned v){ csPut32(p,(uint32_t)v); csPut32(p+4,(uint32_t)(v>>32)); }
static CsInt csSigned(CsUnsigned u){ CsInt i; memcpy(&i,&u,8); return i; }
static int csBit(const CsByte *p,int i){ return (p[i/8]>>(i&7))&1; }
static void csSet(CsByte *p,int i,int v){
  if( v ) p[i/8]|=(CsByte)(1u<<(i&7)); else p[i/8]&=(CsByte)~(1u<<(i&7));
}
static CsInt csBlockId(CsInt id){ return id>=0 ? id/CS_ROWS : -1-(-(id+1))/CS_ROWS; }
static int csSlot(CsInt id){ return (int)((CsUnsigned)id&(CS_ROWS-1)); }
static CsInt csRowid(CsInt block,int slot){ return block*CS_ROWS+slot; }
static CsWide csAdd(CsWide a,CsWide b){
  CsWide r;
  r.lo=a.lo+b.lo; r.hi=csSigned((CsUnsigned)a.hi+(CsUnsigned)b.hi+(r.lo<a.lo));
  return r;
}
static CsWide csWide(CsInt i){ CsWide r; r.lo=(CsUnsigned)i; r.hi=i<0 ? -1 : 0; return r; }
static int csWideCmp(CsWide a,CsWide b){
  return a.hi<b.hi ? -1 : a.hi>b.hi ? 1 : a.lo<b.lo ? -1 : a.lo>b.lo;
}
static int csFits(CsWide a){ return a.hi==(csSigned(a.lo)<0 ? -1 : 0); }
static int csCellCmp(const CsCell *a,const CsCell *b){
  int cmp,n;
  if( a->type==SQLITE_INTEGER ) return a->v.i<b->v.i ? -1 : a->v.i>b->v.i;
  if( a->type==SQLITE_FLOAT ) return a->v.r<b->v.r ? -1 : a->v.r>b->v.r;
  n=a->n<b->n ? a->n : b->n;
  cmp=n ? memcmp(a->v.p,b->v.p,(size_t)n) : 0;
  return cmp ? cmp : a->n<b->n ? -1 : a->n>b->n;
}
static void csRegistryRelease(void*);
static void csCellClear(CsCell *c){
  if( (c->type==SQLITE_TEXT || c->type==SQLITE_BLOB) && c->v.p ){
    CsBytes *p=((CsBytes*)c->v.p)-1;
    if( --p->refs==0 ){
      CsRegistry *r=p->registry;
      r->bytes-=p->size; sqlite3_free(p); csRegistryRelease(r);
    }
  }
  memset(c,0,sizeof(*c));
}
static int csCellEqual(const CsCell *a,const CsCell *b){
  return a->type==b->type && (!a->type || csCellCmp(a,b)==0);
}
static int csCellCopy(CsCell *out,const CsCell *in){
  *out=*in;
  if( in->type==SQLITE_TEXT || in->type==SQLITE_BLOB ){
    CsBytes *p=((CsBytes*)in->v.p)-1;
    p->refs++;
  }
  return SQLITE_OK;
}
static int csCellBytes(CsTable *t,CsCell *out,const void *data,int n){
  CsRegistry *r=t->registry;
  CsUnsigned size=sizeof(CsBytes)+(CsUnsigned)n+1;
  CsBytes *p;
  if( size>r->byteLimit || r->bytes>r->byteLimit-size ) return SQLITE_NOMEM;
  p=(CsBytes*)sqlite3_malloc64(size);
  if( !p ) return SQLITE_NOMEM;
  p->refs=1; p->size=size; p->registry=r;
  r->refs++; r->bytes+=size;
  if( r->bytes>r->peakBytes ) r->peakBytes=r->bytes;
  out->v.p=(CsByte*)(p+1); out->n=n;
  if( n ) memcpy(out->v.p,data,(size_t)n);
  out->v.p[n]=0;
  return SQLITE_OK;
}
static int csFromValue(CsTable *t,CsCell *out,sqlite3_value *v,int type){
  int actual=sqlite3_value_type(v);
  memset(out,0,sizeof(*out));
  if( actual==SQLITE_NULL ) return SQLITE_OK;
  if( type==SQLITE_FLOAT && actual==SQLITE_INTEGER ) actual=SQLITE_FLOAT;
  if( actual!=type ) return SQLITE_CONSTRAINT_DATATYPE;
  out->type=type;
  if( type==SQLITE_INTEGER ) out->v.i=sqlite3_value_int64(v);
  else if( type==SQLITE_FLOAT ){
    out->v.r=sqlite3_value_double(v);
    if( !(out->v.r<=DBL_MAX && out->v.r>=-DBL_MAX) ) return SQLITE_CONSTRAINT_DATATYPE;
  }else{
    const void *p=type==SQLITE_TEXT ? (const void*)sqlite3_value_text(v) : sqlite3_value_blob(v);
    out->n=sqlite3_value_bytes(v);
    if( !p && (out->n || type==SQLITE_TEXT) ) return SQLITE_NOMEM;
    return csCellBytes(t,out,p,out->n);
  }
  return SQLITE_OK;
}
static int csBind(sqlite3_stmt *s,int i,const CsCell *c){
  if( !c || !c->type ) return sqlite3_bind_null(s,i);
  if( c->type==SQLITE_INTEGER ) return sqlite3_bind_int64(s,i,c->v.i);
  if( c->type==SQLITE_FLOAT ) return sqlite3_bind_double(s,i,c->v.r);
  if( c->type==SQLITE_TEXT ) return sqlite3_bind_text(s,i,(const char*)c->v.p,c->n,SQLITE_TRANSIENT);
  return sqlite3_bind_blob(s,i,c->v.p,c->n,SQLITE_TRANSIENT);
}
static void csResult(sqlite3_context *ctx,const CsCell *c){
  if( !c || !c->type ) sqlite3_result_null(ctx);
  else if( c->type==SQLITE_INTEGER ) sqlite3_result_int64(ctx,c->v.i);
  else if( c->type==SQLITE_FLOAT ) sqlite3_result_double(ctx,c->v.r);
  else if( c->type==SQLITE_TEXT ) sqlite3_result_text(ctx,(const char*)c->v.p,c->n,SQLITE_TRANSIENT);
  else sqlite3_result_blob(ctx,c->v.p,c->n,SQLITE_TRANSIENT);
}
static CsBlock *csNewBlock(CsTable *t,CsInt id){
  CsBlock *b=(CsBlock*)sqlite3_malloc64(sizeof(*b));
  (void)t;
  if( !b ) return 0;
  memset(b,0,sizeof(*b)); b->id=id;
  return b;
}
static int csAllocateColumn(CsBlock *b,int col){
  if( b->cells[col] ) return SQLITE_OK;
  b->cells[col]=(CsCell*)sqlite3_malloc64(CS_ROWS*sizeof(CsCell));
  if( !b->cells[col] ) return SQLITE_NOMEM;
  memset(b->cells[col],0,CS_ROWS*sizeof(CsCell));
  return SQLITE_OK;
}
static void csFreeBlock(CsTable *t,CsBlock *b){
  int i,j;
  if( !b ) return;
  for(i=0;i<t->ncol;i++) if( b->cells[i] ){
    for(j=0;j<CS_ROWS;j++) csCellClear(&b->cells[i][j]);
    sqlite3_free(b->cells[i]);
  }
  sqlite3_free(b);
}
static CsBlock *csCopyBlock(CsTable *t,CsBlock *src){
  CsBlock *b;
  int i,j;
  if( !src ) return 0;
  b=csNewBlock(t,src->id);
  if( !b ) return 0;
  memcpy(b->live,src->live,CS_MAP); b->count=src->count;
  b->dirty=src->dirty; b->loaded=src->loaded;
  b->changed=src->changed;
  b->persisted=src->persisted;
  for(i=0;i<t->ncol;i++) if( src->cells[i] ){
    if( csAllocateColumn(b,i) ){ csFreeBlock(t,b); return 0; }
    for(j=0;j<CS_ROWS;j++){
      if( csCellCopy(&b->cells[i][j],&src->cells[i][j]) ){ csFreeBlock(t,b); return 0; }
    }
  }
  return b;
}
static CsStats csDescribe(CsBlock *b,int col){
  CsStats s;
  int i;
  memset(&s,0,sizeof(s));
  for(i=0;i<CS_ROWS;i++){
    CsCell *c=&b->cells[col][i];
    if( !csBit(b->live,i) || !c->type ) continue;
    s.count++;
    if( !s.min || csCellCmp(c,s.min)<0 ) s.min=c;
    if( !s.max || csCellCmp(c,s.max)>0 ) s.max=c;
    if( c->type==SQLITE_INTEGER ){
      s.sum=csAdd(s.sum,csWide(c->v.i));
      if( csWideCmp(s.sum,s.low)<0 ) s.low=s.sum;
      if( csWideCmp(s.sum,s.high)>0 ) s.high=s.sum;
    }
  }
  return s;
}
static int csGrow(CsBuffer *b,int n){
  int cap;
  CsByte *p;
  if( b->rc ) return b->rc;
  if( n<0 || n>CS_LIMIT-b->n ) return b->rc=SQLITE_TOOBIG;
  if( b->n+n<=b->cap ) return SQLITE_OK;
  cap=b->cap ? b->cap : 256;
  while( cap<b->n+n ){ if( cap>CS_LIMIT/2 ){ cap=CS_LIMIT; break; } cap*=2; }
  p=(CsByte*)sqlite3_realloc64(b->p,(CsUnsigned)cap);
  if( !p ) return b->rc=SQLITE_NOMEM;
  b->p=p; b->cap=cap; return SQLITE_OK;
}
static void csAppend(CsBuffer *b,const void *p,int n){
  if( csGrow(b,n) ) return;
  if( n ) memcpy(b->p+b->n,p,(size_t)n);
  b->n+=n;
}
static int csWidth(CsUnsigned v){ int w=0; while( v ){ w++; v>>=1; } return w; }
static void csPutBits(CsByte *p,int bit,int width,CsUnsigned value){
  int shift=bit&7,take;
  p+=bit/8;
  while( width ){
    take=8-shift; if( take>width ) take=width;
    *p++|=(CsByte)((value&((1u<<take)-1))<<shift);
    value>>=take; width-=take; shift=0;
  }
}
static CsUnsigned csGetBits(const CsByte *p,int bit,int width){
  CsUnsigned value=0;
  int shift=bit&7,take,done=0;
  p+=bit/8;
  while( width ){
    take=8-shift; if( take>width ) take=width;
    value|=(CsUnsigned)((*p++>>shift)&((1u<<take)-1))<<done;
    done+=take; width-=take; shift=0;
  }
  return value;
}
static int csAffine(CsCell *a,CsInt *base,CsInt *step,int *first){
  int i,second=-1;
  CsWide diff,cur,negative;
  *first=-1; *step=0;
  for(i=0;i<CS_ROWS;i++) if( a[i].type ){
    if( *first<0 ) *first=i; else { second=i; break; }
  }
  if( *first<0 ) return 0;
  *base=a[*first].v.i;
  if( second<0 ) return 1;
  /* Subtraction in two limbs, including INT64_MIN. */
  negative.lo=~(CsUnsigned)*base; negative.hi=~(*base<0 ? (CsInt)-1 : (CsInt)0);
  diff=csAdd(csWide(a[second].v.i),negative);
  diff=csAdd(diff,csWide(1));
  if( !csFits(diff) || csSigned(diff.lo)%(second-*first) ) return 0;
  *step=csSigned(diff.lo)/(second-*first);
  cur=csWide(*base);
  for(i=*first;i<CS_ROWS;i++){
    if( a[i].type && (!csFits(cur) || a[i].v.i!=csSigned(cur.lo)) ) return 0;
    cur=csAdd(cur,csWide(*step));
  }
  return 1;
}
static int csEncodeRaw(CsTable *t,CsBlock *block,int col,const CsStats *description,CsBuffer *out){
  CsCell *a=block->cells[col];
  CsByte header[CS_HEADER],nulls[CS_MAP];
  CsStats stats=*description;
  int i,type=t->types[col],width=0,first=0;
  CsInt base=0,step=0;
  memset(header,0,sizeof(header)); memset(nulls,0,sizeof(nulls));
  memcpy(header,"CSC1",4); header[4]=(CsByte)type;
  header[7]=(CsByte)(stats.count==0 ? 2 : stats.count==CS_ROWS ? 0 : 1);
  csPut32(header+28,CS_ROWS);
  for(i=0;i<CS_ROWS;i++) if( !a[i].type ) csSet(nulls,i,1);
  if( type==SQLITE_INTEGER && stats.count ){
    base=stats.min->v.i;
    width=csWidth((CsUnsigned)stats.max->v.i-(CsUnsigned)base);
    if( width && csAffine(a,&base,&step,&first) ){
      header[5]=1; csPut64(header+8,(CsUnsigned)base);
      csPut64(header+16,(CsUnsigned)step); csPut32(header+24,(uint32_t)first);
    }else{
      base=stats.min->v.i;
      header[6]=(CsByte)width; csPut64(header+8,(CsUnsigned)base);
      csPut64(header+16,(CsUnsigned)stats.max->v.i);
    }
  }
  csAppend(out,header,CS_HEADER);
  if( header[7]==1 ) csAppend(out,nulls,CS_MAP);
  if( out->rc || !stats.count ) return out->rc;
  if( type==SQLITE_INTEGER ){
    if( !header[5] ){
      int n=(CS_ROWS*width+7)/8;
      if( csGrow(out,n) ) return out->rc;
      memset(out->p+out->n,0,(size_t)n);
      for(i=0;i<CS_ROWS;i++) if( a[i].type ){
        csPutBits(out->p+out->n,i*width,width,(CsUnsigned)a[i].v.i-(CsUnsigned)base);
      }
      out->n+=n;
    }
  }else if( type==SQLITE_FLOAT ){
    for(i=0;i<CS_ROWS;i++) if( a[i].type ){
      CsUnsigned bits; CsByte bytes[8];
      memcpy(&bits,&a[i].v.r,8); csPut64(bytes,bits); csAppend(out,bytes,8);
    }
  }else{
    int slots[2048],codes[CS_ROWS],unique[CS_ROWS],ndict=0;
    CsUnsigned raw=0,dict=4;
    memset(slots,0,sizeof(slots)); memset(codes,0,sizeof(codes));
    for(i=0;i<CS_ROWS;i++) if( a[i].type ){
      uint32_t h=2166136261u;
      int j,slot;
      raw+=(CsUnsigned)a[i].n+4;
      for(j=0;j<a[i].n;j++) h=(h^a[i].v.p[j])*16777619u;
      slot=(int)(h&2047);
      while( slots[slot] && csCellCmp(&a[i],&a[unique[slots[slot]-1]]) ) slot=(slot+1)&2047;
      if( !slots[slot] ){
        unique[ndict]=i; slots[slot]=++ndict; dict+=(CsUnsigned)a[i].n+4;
      }
      codes[i]=slots[slot]-1;
    }
    width=csWidth((CsUnsigned)ndict-1);
    dict+=(CS_ROWS*width+7)/8;
    if( dict<raw ){
      CsByte bytes[4];
      int n=(CS_ROWS*width+7)/8;
      out->p[5]=1; out->p[6]=(CsByte)width;
      csPut32(bytes,(uint32_t)ndict); csAppend(out,bytes,4);
      for(i=0;i<ndict;i++){
        CsCell *c=&a[unique[i]];
        csPut32(bytes,(uint32_t)c->n); csAppend(out,bytes,4); csAppend(out,c->v.p,c->n);
      }
      if( csGrow(out,n) ) return out->rc;
      memset(out->p+out->n,0,(size_t)n);
      for(i=0;i<CS_ROWS;i++) csPutBits(out->p+out->n,i*width,width,(CsUnsigned)codes[i]);
      out->n+=n;
    }else{
      for(i=0;i<CS_ROWS;i++) if( a[i].type ){
        CsByte bytes[4];
        csPut32(bytes,(uint32_t)a[i].n); csAppend(out,bytes,4); csAppend(out,a[i].v.p,a[i].n);
      }
    }
  }
  return out->rc;
}
extern int sqlnew_pack_buffer(sqlite3*,const void*,int,void**,int*);
extern int sqlnew_pack_buffer_limit(sqlite3*,const void*,int,int,void**,int*);
extern int sqlnew_fold_pack(const void*,int,CsUnsigned,void**,int*);
extern int sqlnew_fold_unpack(const void*,int,void**,int*);
extern int sqlnew_fold_probe(const void*,int);
/* Keep the old representation unless a complete folded frame saves at least
** 10% and 16 bytes. The inexpensive uncompressed-size gate avoids recompressing
** weak candidates. Existing affine and all-NULL frames retain their encoding. */
static int csEncode(CsTable *t,CsBlock *block,int col,const CsStats *stats,CsBuffer *out){
  CsBuffer raw,alternative;
  CsByte words[CS_ROWS*8];
  void *packed=0,*folded=0,*candidate=0;
  int size=0,foldSize=0,candidateSize=0,rc,i,n=0,header,stop=0,probe,probed;
  int type=t->types[col];
  memset(&raw,0,sizeof(raw)); memset(&alternative,0,sizeof(alternative));
  rc=csEncodeRaw(t,block,col,stats,&raw);
  if( rc ) goto done;
  if( !t->registry->folding || (type!=SQLITE_INTEGER && type!=SQLITE_FLOAT) || stats->count<32
      || raw.n<160 || (type==SQLITE_INTEGER && raw.p[5]) ) goto legacy;
  for(i=0;i<CS_ROWS;i++) if( block->cells[col][i].type ){
    CsUnsigned bits;
    if( type==SQLITE_INTEGER ) bits=(CsUnsigned)block->cells[col][i].v.i;
    else memcpy(&bits,&block->cells[col][i].v.r,8);
    csPut64(words+n,bits); n+=8;
  }
  probed=stats->count<128 ? stats->count : 128;
  probe=sqlnew_fold_probe(words,probed*8);
  if( probe<0 || 2*probe*stats->count>raw.n*probed ) goto legacy;
  rc=sqlnew_fold_pack(words,n,0,&folded,&foldSize);
  if( rc ) goto done;
  header=CS_HEADER+(raw.p[7]==1 ? CS_MAP : 0);
  if( 2*(header+foldSize)>raw.n ) goto legacy;
  csAppend(&alternative,raw.p,header);
  csAppend(&alternative,folded,foldSize);
  if( alternative.rc ){ rc=alternative.rc; goto done; }
  alternative.p[5]=2; alternative.p[6]=0;
  memset(alternative.p+8,0,20);
  rc=sqlnew_pack_buffer(t->db,alternative.p,alternative.n,&candidate,&candidateSize);
  if( rc ) goto done;
  stop=(candidateSize*10+8)/9;
  if( stop<candidateSize+16 ) stop=candidateSize+16;
legacy:
  rc=sqlnew_pack_buffer_limit(t->db,raw.p,raw.n,stop,&packed,&size);
  if( rc==SQLITE_DONE ){
    rc=SQLITE_OK; packed=candidate; size=candidateSize; candidate=0;
  }else if( rc ) goto done;
  else if( candidate && candidateSize+16<=size && candidateSize*10<=size*9 ){
    sqlite3_free(packed); packed=candidate; size=candidateSize; candidate=0;
  }
  out->p=(CsByte*)packed; out->n=size; out->cap=size; packed=0;
done:
  sqlite3_free(raw.p); sqlite3_free(alternative.p);
  sqlite3_free(packed); sqlite3_free(folded); sqlite3_free(candidate);
  out->rc=rc; return rc;
}
static int csDecode(CsTable *t,CsBlock *block,int col,const CsByte *p,int bytes){
  CsCell *a=block->cells[col];
  const CsByte *nulls=0,*end=p+bytes,*payload,*dictionary[CS_ROWS];
  int lengths[CS_ROWS],owners[CS_ROWS],i,kind,codec,width,nullmode,ndict=0,first;
  CsInt base,maximum,step;
  CsWide cur;
  if( bytes<CS_HEADER || memcmp(p,"CSC1",4) || csGet32(p+28)!=CS_ROWS ) return SQLITE_CORRUPT_VTAB;
  kind=p[4]; codec=p[5]; width=p[6]; nullmode=p[7];
  if( kind!=t->types[col] || codec>2 || width>64 || nullmode>2 ) return SQLITE_CORRUPT_VTAB;
  base=csSigned(csGet64(p+8)); maximum=csSigned(csGet64(p+16));
  step=maximum; first=(int)csGet32(p+24);
  p+=CS_HEADER;
  if( nullmode==1 ){
    if( end-p<CS_MAP ) return SQLITE_CORRUPT_VTAB;
    nulls=p; p+=CS_MAP;
  }
  if( nullmode==2 ) return p==end ? SQLITE_OK : SQLITE_CORRUPT_VTAB;
  if( codec==2 ){
    void *values=0;
    const CsByte *words;
    int n=0,size=0,rc;
    if( (kind!=SQLITE_INTEGER && kind!=SQLITE_FLOAT) || width ) return SQLITE_CORRUPT_VTAB;
    for(i=0;i<CS_ROWS;i++) if( !nulls || !csBit(nulls,i) ) n++;
    rc=sqlnew_fold_unpack(p,(int)(end-p),&values,&size);
    if( rc ) return rc==SQLITE_NOMEM ? rc : SQLITE_CORRUPT_VTAB;
    if( size!=n*8 ){ sqlite3_free(values); return SQLITE_CORRUPT_VTAB; }
    words=(const CsByte*)values;
    for(i=0;i<CS_ROWS;i++) if( !nulls || !csBit(nulls,i) ){
      CsUnsigned bits=csGet64(words); words+=8; a[i].type=kind;
      if( kind==SQLITE_INTEGER ) a[i].v.i=csSigned(bits);
      else{
        memcpy(&a[i].v.r,&bits,8);
        if( !(a[i].v.r<=DBL_MAX && a[i].v.r>=-DBL_MAX) ){
          sqlite3_free(values); return SQLITE_CORRUPT_VTAB;
        }
      }
    }
    sqlite3_free(values); return SQLITE_OK;
  }
  payload=p; cur=csWide(base);
  if( kind==SQLITE_INTEGER ){
    if( !codec ){
      if( maximum<base || width!=csWidth((CsUnsigned)maximum-(CsUnsigned)base)
          || end-p!=(CS_ROWS*width+7)/8 ) return SQLITE_CORRUPT_VTAB;
    }else if( p!=end || first<0 || first>=CS_ROWS || width ) return SQLITE_CORRUPT_VTAB;
  }else if( kind==SQLITE_FLOAT ){
    if( codec || width ) return SQLITE_CORRUPT_VTAB;
  }else if( codec ){
    if( end-p<4 ) return SQLITE_CORRUPT_VTAB;
    ndict=(int)csGet32(p); p+=4;
    if( ndict<1 || ndict>CS_ROWS || width!=csWidth((CsUnsigned)ndict-1) ) return SQLITE_CORRUPT_VTAB;
    for(i=0;i<ndict;i++){
      uint32_t n;
      if( end-p<4 ) return SQLITE_CORRUPT_VTAB;
      n=csGet32(p); p+=4;
      if( n>(CsUnsigned)(end-p) ) return SQLITE_CORRUPT_VTAB;
      dictionary[i]=p; lengths[i]=(int)n; owners[i]=-1; p+=n;
    }
    payload=p;
    if( end-p!=(CS_ROWS*width+7)/8 ) return SQLITE_CORRUPT_VTAB;
  }else if( width ) return SQLITE_CORRUPT_VTAB;
  for(i=0;i<CS_ROWS;i++){
    int isNull=nulls && csBit(nulls,i);
    if( kind==SQLITE_INTEGER && codec && i>=first ){
      if( !isNull && !csFits(cur) ) return SQLITE_CORRUPT_VTAB;
      if( !isNull ) a[i].v.i=csSigned(cur.lo);
      cur=csAdd(cur,csWide(step));
    }
    if( isNull ) continue;
    a[i].type=kind;
    if( kind==SQLITE_INTEGER ){
      if( codec ){ if( i<first ) return SQLITE_CORRUPT_VTAB; }
      else{
        CsUnsigned offset=csGetBits(payload,i*width,width);
        if( offset>(CsUnsigned)maximum-(CsUnsigned)base ) return SQLITE_CORRUPT_VTAB;
        a[i].v.i=csSigned((CsUnsigned)base+offset);
      }
    }else if( kind==SQLITE_FLOAT ){
      CsUnsigned bits;
      if( end-p<8 ) return SQLITE_CORRUPT_VTAB;
      bits=csGet64(p); p+=8; memcpy(&a[i].v.r,&bits,8);
      if( !(a[i].v.r<=DBL_MAX && a[i].v.r>=-DBL_MAX) ) return SQLITE_CORRUPT_VTAB;
    }else{
      const CsByte *data;
      int n,rc;
      if( codec ){
        CsUnsigned code=csGetBits(payload,i*width,width);
        if( code>=(CsUnsigned)ndict ) return SQLITE_CORRUPT_VTAB;
        if( owners[code]>=0 ){
          rc=csCellCopy(&a[i],&a[owners[code]]);
          if( rc ) return rc;
          continue;
        }
        owners[code]=i;
        data=dictionary[code]; n=lengths[code];
      }else{
        uint32_t size;
        if( end-p<4 ) return SQLITE_CORRUPT_VTAB;
        size=csGet32(p); p+=4;
        if( size>(CsUnsigned)(end-p) ) return SQLITE_CORRUPT_VTAB;
        n=(int)size; data=p; p+=size;
      }
      rc=csCellBytes(t,&a[i],data,n);
      if( rc ) return rc;
    }
  }
  if( kind==SQLITE_FLOAT || ((kind==SQLITE_TEXT || kind==SQLITE_BLOB) && !codec) ){
    if( p!=end ) return SQLITE_CORRUPT_VTAB;
  }
  return SQLITE_OK;
}

static int csPrepare(CsTable *t,sqlite3_stmt **s,char *sql){
  int rc;
  if( !sql ) return SQLITE_NOMEM;
  rc=sqlite3_prepare_v2(t->db,sql,-1,s,0);
  sqlite3_free(sql);
  return rc;
}
static int csExec(CsTable *t,char *sql){
  int rc;
  CsInt last=sqlite3_last_insert_rowid(t->db);
  if( !sql ) return SQLITE_NOMEM;
  rc=sqlite3_exec(t->db,sql,0,0,0);
  sqlite3_set_last_insert_rowid(t->db,last);
  sqlite3_free(sql); return rc;
}
static int csDone(sqlite3_stmt *s){
  int rc=sqlite3_step(s),end=sqlite3_finalize(s);
  return rc==SQLITE_DONE ? end : rc;
}
static void csIndexesFree(CsIndex*);
static int csIndexesLoad(CsTable*,CsIndex**);
static int csIndexBest(CsTable*,sqlite3_index_info*);
static int csIndexFilter(CsCursor*,int,const char*,int,sqlite3_value**);
static int csIndexChange(CsTable*,CsInt,CsCell*,CsUnsigned);
static int csIndexRow(CsTable*,CsInt,CsCell**,int);
static int csIndexSchema(CsTable*,const char*);
static int csError(CsTable *t,int rc,const char *message){
  sqlite3_free(t->base.zErrMsg); t->base.zErrMsg=sqlite3_mprintf("%s",message);
  return rc;
}
static int csApplyPatches(CsTable*,CsBlock*,int);
static int csLoadColumn(CsTable *t,CsBlock *b,int col,sqlite3_stmt *s){
  int rc;
  if( b->loaded&((CsUnsigned)1<<col) ) return SQLITE_OK;
  rc=csAllocateColumn(b,col);
  if( rc ) return rc;
  sqlite3_reset(s); sqlite3_clear_bindings(s);
  sqlite3_bind_int64(s,1,b->id); sqlite3_bind_int(s,2,col);
  rc=sqlite3_step(s);
  if( rc==SQLITE_ROW ){
    const CsByte *p=(const CsByte*)sqlite3_column_blob(s,1);
    int bytes=sqlite3_column_bytes(s,1);
    if( !p ) rc=SQLITE_CORRUPT_VTAB;
    else{
      t->columnsRead++; t->bytesRead+=(CsUnsigned)sqlite3_column_int64(s,0);
      rc=csDecode(t,b,col,p,bytes);
    }
    if( rc==SQLITE_OK ) rc=csApplyPatches(t,b,col);
    if( rc==SQLITE_OK ) b->loaded|=(CsUnsigned)1<<col;
  }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  sqlite3_reset(s);
  return rc;
}
static int csColumnStatement(CsTable *t,sqlite3_stmt **s){
  return csPrepare(t,s,sqlite3_mprintf(
    "SELECT length(data),sqlnew_unpack(data) FROM \"%w\".\"%w_data\" WHERE block=? AND col=?",
    t->schema,t->name));
}
static int csApplyPatches(CsTable *t,CsBlock *b,int col){
  sqlite3_stmt *s;
  int rc=SQLITE_OK;
  if( !t->patchRead ) rc=csPrepare(t,&t->patchRead,sqlite3_mprintf(
    "SELECT slot,value,original FROM \"%w\".\"%w_patch\" WHERE col=? AND block=? ORDER BY slot",t->schema,t->name));
  if( rc ) return rc;
  s=t->patchRead;
  sqlite3_bind_int(s,1,col); sqlite3_bind_int64(s,2,b->id);
  while( (rc=sqlite3_step(s))==SQLITE_ROW ){
    CsCell original,value;
    int slot=sqlite3_column_int(s,0);
    memset(&original,0,sizeof(original)); memset(&value,0,sizeof(value));
    if( slot<0 || slot>=CS_ROWS || !csBit(b->live,slot) ){ rc=SQLITE_CORRUPT_VTAB; break; }
    rc=csFromValue(t,&original,sqlite3_column_value(s,2),t->types[col]);
    if( !rc && !csCellEqual(&original,&b->cells[col][slot]) ) rc=SQLITE_CORRUPT_VTAB;
    if( !rc ) rc=csFromValue(t,&value,sqlite3_column_value(s,1),t->types[col]);
    csCellClear(&original);
    if( rc ){ csCellClear(&value); break; }
    csCellClear(&b->cells[col][slot]); b->cells[col][slot]=value; t->patchesRead++;
  }
  sqlite3_reset(s); sqlite3_clear_bindings(s);
  return rc==SQLITE_DONE ? SQLITE_OK : rc;
}
static int csReadLive(CsTable *t,CsBlock *b,sqlite3_stmt *s,int liveCol,int countCol){
  const CsByte *p=(const CsByte*)sqlite3_column_blob(s,liveCol);
  int i,n=0;
  (void)t;
  if( !p || sqlite3_column_bytes(s,liveCol)!=CS_MAP ) return SQLITE_CORRUPT_VTAB;
  memcpy(b->live,p,CS_MAP); b->count=sqlite3_column_int(s,countCol);
  b->persisted=1;
  for(i=0;i<CS_ROWS;i++) n+=csBit(b->live,i);
  if( n!=b->count || !n ) return SQLITE_CORRUPT_VTAB;
  if( b->id<csBlockId(INT64_MIN) || b->id>csBlockId(INT64_MAX) ) return SQLITE_CORRUPT_VTAB;
  return SQLITE_OK;
}
static int csFlushData(CsTable *t){
  CsBlock *b=t->write;
  sqlite3_stmt *s=0,*payload=0,*patch=0;
  int rc,i;
  if( !b || !b->dirty ) return SQLITE_OK;
  if( !b->count ){
    rc=csPrepare(t,&s,sqlite3_mprintf("DELETE FROM \"%w\".\"%w_patch\" WHERE block=?",t->schema,t->name));
    if( rc ) return rc;
    sqlite3_bind_int64(s,1,b->id); rc=csDone(s);
    if( rc ) return rc;
    rc=csPrepare(t,&s,sqlite3_mprintf("DELETE FROM \"%w\".\"%w_data\" WHERE block=?",t->schema,t->name));
    if( rc ) return rc;
    sqlite3_bind_int64(s,1,b->id); rc=csDone(s);
    if( rc ) return rc;
    rc=csPrepare(t,&s,sqlite3_mprintf("DELETE FROM \"%w\".\"%w_columns\" WHERE block=?",t->schema,t->name));
    if( rc ) return rc;
    sqlite3_bind_int64(s,1,b->id); rc=csDone(s);
    if( rc ) return rc;
    rc=csPrepare(t,&s,sqlite3_mprintf("DELETE FROM \"%w\".\"%w_blocks\" WHERE block=?",t->schema,t->name));
    if( rc ) return rc;
    sqlite3_bind_int64(s,1,b->id); rc=csDone(s);
    if( !rc ) b->dirty=0;
    return rc;
  }
  rc=csPrepare(t,&s,sqlite3_mprintf(
    "INSERT INTO \"%w\".\"%w_columns\" VALUES(?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(block,col) DO UPDATE SET minimum=excluded.minimum,"
    "maximum=excluded.maximum,nonnull=excluded.nonnull,sumlo=excluded.sumlo,sumhi=excluded.sumhi,"
    "lowlo=excluded.lowlo,lowhi=excluded.lowhi,highlo=excluded.highlo,highhi=excluded.highhi",
    t->schema,t->name));
  if( rc ) return rc;
  rc=csPrepare(t,&payload,sqlite3_mprintf(
    "INSERT INTO \"%w\".\"%w_data\" VALUES(?,?,?)"
    " ON CONFLICT(block,col) DO UPDATE SET data=excluded.data",t->schema,t->name));
  if( rc ){ sqlite3_finalize(s); return rc; }
  if( b->persisted ) rc=csPrepare(t,&patch,sqlite3_mprintf(
    "DELETE FROM \"%w\".\"%w_patch\" WHERE col=? AND block=?",t->schema,t->name));
  if( rc ){ sqlite3_finalize(s); sqlite3_finalize(payload); return rc; }
  for(i=0;i<t->ncol;i++){
    CsBuffer data;
    CsStats stats;
    if( !(b->changed&((CsUnsigned)1<<i)) ) continue;
    stats=csDescribe(b,i);
    memset(&data,0,sizeof(data));
    rc=csEncode(t,b,i,&stats,&data);
    if( rc ){ sqlite3_free(data.p); break; }
    sqlite3_bind_int64(payload,1,b->id); sqlite3_bind_int(payload,2,i);
    rc=sqlite3_bind_blob(payload,3,data.p,data.n,sqlite3_free);
    if( rc ) break;
    rc=sqlite3_step(payload);
    if( rc!=SQLITE_DONE ) break;
    rc=sqlite3_reset(payload); sqlite3_clear_bindings(payload);
    sqlite3_bind_int64(s,1,b->id); sqlite3_bind_int(s,2,i);
    if( !rc ) rc=csBind(s,3,stats.min);
    if( !rc ) rc=csBind(s,4,stats.max);
    if( !rc ) rc=sqlite3_bind_int(s,5,stats.count);
    if( !rc ) rc=sqlite3_bind_int64(s,6,csSigned(stats.sum.lo));
    if( !rc ) rc=sqlite3_bind_int64(s,7,stats.sum.hi);
    if( !rc ) rc=sqlite3_bind_int64(s,8,csSigned(stats.low.lo));
    if( !rc ) rc=sqlite3_bind_int64(s,9,stats.low.hi);
    if( !rc ) rc=sqlite3_bind_int64(s,10,csSigned(stats.high.lo));
    if( !rc ) rc=sqlite3_bind_int64(s,11,stats.high.hi);
    if( !rc ) rc=sqlite3_step(s);
    if( rc!=SQLITE_DONE ) break;
    rc=sqlite3_reset(s); sqlite3_clear_bindings(s);
    if( rc ) break;
    if( patch ){
      sqlite3_bind_int(patch,1,i); sqlite3_bind_int64(patch,2,b->id);
      rc=sqlite3_step(patch);
      if( rc!=SQLITE_DONE ) break;
      rc=sqlite3_reset(patch); sqlite3_clear_bindings(patch);
      if( rc ) break;
    }
    t->columnsWritten++;
  }
  sqlite3_finalize(s); sqlite3_finalize(payload); sqlite3_finalize(patch);
  if( rc ) return rc;
  rc=csPrepare(t,&s,sqlite3_mprintf(
    "INSERT INTO \"%w\".\"%w_blocks\" VALUES(?,?,?) ON CONFLICT(block) DO UPDATE SET live=excluded.live,n=excluded.n",
    t->schema,t->name));
  if( rc ) return rc;
  sqlite3_bind_int64(s,1,b->id);
  rc=sqlite3_bind_blob(s,2,b->live,CS_MAP,SQLITE_TRANSIENT); sqlite3_bind_int(s,3,b->count);
  if( !rc ) rc=csDone(s); else sqlite3_finalize(s);
  if( !rc ){ b->dirty=0; b->changed=0; b->persisted=1; t->blocksWritten++; }
  return rc;
}
static int csFlush(CsTable *t){
  CsInt last=sqlite3_last_insert_rowid(t->db);
  int rc=csFlushData(t);
  /* Shadow-table rowids must not replace the caller's inserted rowid. */
  sqlite3_set_last_insert_rowid(t->db,last);
  return rc;
}
static int csWriteBlock(CsTable *t,CsInt id){
  sqlite3_stmt *s=0,*column=0;
  CsBlock *b;
  int rc,i;
  if( t->write && t->write->id==id ) return SQLITE_OK;
  rc=csFlush(t);
  if( rc ) return rc;
  b=csNewBlock(t,id);
  if( !b ) return SQLITE_NOMEM;
  rc=csPrepare(t,&s,sqlite3_mprintf("SELECT live,n FROM \"%w\".\"%w_blocks\" WHERE block=?",t->schema,t->name));
  if( !rc ){
    sqlite3_bind_int64(s,1,id); rc=sqlite3_step(s);
    if( rc==SQLITE_ROW ){
      rc=csReadLive(t,b,s,0,1);
      if( !rc ) rc=csColumnStatement(t,&column);
      for(i=0;!rc && i<t->ncol;i++) rc=csLoadColumn(t,b,i,column);
    }else if( rc==SQLITE_DONE ) rc=SQLITE_OK;
  }
  sqlite3_finalize(s); sqlite3_finalize(column);
  for(i=0;!rc && i<t->ncol;i++) rc=csAllocateColumn(b,i);
  if( rc ){ csFreeBlock(t,b); return rc; }
  b->loaded=((CsUnsigned)1<<t->ncol)-1;
  csFreeBlock(t,t->write); t->write=b; return SQLITE_OK;
}
static int csBegin(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  sqlite3_stmt *s=0;
  int rc;
  if( t->busy ){ t->reset=2; return SQLITE_OK; }
  csFreeBlock(t,t->write); t->write=0;
  rc=csPrepare(t,&s,sqlite3_mprintf("SELECT next,full FROM \"%w\".\"%w_meta\" WHERE id=1",t->schema,t->name));
  if( rc ) return rc;
  rc=sqlite3_step(s);
  if( rc==SQLITE_ROW ){
    t->next=sqlite3_column_int64(s,0); t->full=sqlite3_column_int(s,1);
    t->inWrite=1; rc=SQLITE_OK;
  }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  sqlite3_finalize(s);
  csIndexesFree(t->indexes); t->indexes=0;
  if( !rc ) rc=csIndexesLoad(t,&t->indexes);
  return rc;
}
static int csLeave(CsTable*,int);
static int csSyncBody(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  sqlite3_stmt *s=0;
  int rc=csFlush(t);
  if( rc || !t->inWrite ) return rc;
  rc=csPrepare(t,&s,sqlite3_mprintf("UPDATE \"%w\".\"%w_meta\" SET next=?,full=? WHERE id=1",t->schema,t->name));
  if( rc ) return rc;
  sqlite3_bind_int64(s,1,t->next); sqlite3_bind_int(s,2,t->full);
  return csDone(s);
}
static int csSync(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  int rc;
  t->busy++; rc=csSyncBody(v); return csLeave(t,rc);
}
static int csEnd(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  if( t->busy ){ t->reset=1; return SQLITE_OK; }
  csFreeBlock(t,t->write); t->write=0; t->inWrite=0;
  csIndexesFree(t->indexes); t->indexes=0;
  return SQLITE_OK;
}
static int csLeave(CsTable *t,int rc){
  t->busy--;
  if( !t->busy && t->reset ){
    int reset=t->reset,cleanup;
    t->reset=0;
    cleanup=reset==2 ? csBegin(&t->base) : csEnd(&t->base);
    if( !rc ) rc=cleanup;
  }
  return rc;
}
static int csSavepoint(sqlite3_vtab *v,int id){
  /* The engine invokes this BEFORE opening the B-tree savepoint. Persist
  ** pending state there so rollback, including transactional DROP, restores it. */
  (void)id;
  return csSync(v);
}
static int csRelease(sqlite3_vtab *v,int id){ (void)v; (void)id; return SQLITE_OK; }
static int csRollbackTo(sqlite3_vtab *v,int id){
  (void)id;
  return csBegin(v);
}
#include "columnar_write.inc"
static int csCommand(CsTable*,const char*);
static int csUpdateBody(sqlite3_vtab *v,int argc,sqlite3_value **argv,CsInt *rowid){
  CsTable *t=(CsTable*)v;
  CsCell incoming[CS_COLS];
  CsInt old=0,id;
  int i,rc=SQLITE_OK,slot,exists,automatic=0,change=0;
  CsUnsigned changed=0;
  memset(incoming,0,sizeof(incoming));
  if( argc==1 ){
    old=sqlite3_value_int64(argv[0]);
    rc=csWriteBlock(t,csBlockId(old));
    if( rc ) return rc;
    slot=csSlot(old);
    if( csBit(t->write->live,slot) ){
      CsCell *values[CS_COLS];
      for(i=0;i<t->ncol;i++) values[i]=&t->write->cells[i][slot];
      rc=csIndexRow(t,old,values,1);
      if( rc ) return rc;
      csSet(t->write->live,slot,0); t->write->count--; t->write->dirty=1;
      t->write->changed=((CsUnsigned)1<<t->ncol)-1;
      for(i=0;i<t->ncol;i++) csCellClear(&t->write->cells[i][slot]);
    }
    return SQLITE_OK;
  }
  if( argc!=t->ncol+3 ) return SQLITE_MISUSE;
  if( sqlite3_value_type(argv[t->ncol+2])!=SQLITE_NULL ){
    const char *command=(const char*)sqlite3_value_text(argv[t->ncol+2]);
    if( sqlite3_value_type(argv[0])!=SQLITE_NULL ) return SQLITE_CONSTRAINT;
    for(i=1;i<t->ncol+2;i++) if( sqlite3_value_type(argv[i])!=SQLITE_NULL ) return SQLITE_CONSTRAINT;
    *rowid=sqlite3_last_insert_rowid(t->db);
    return command ? csCommand(t,command) : SQLITE_NOMEM;
  }
  change=sqlite3_value_type(argv[0])!=SQLITE_NULL;
  if( change ) old=sqlite3_value_int64(argv[0]);
  automatic=sqlite3_value_type(argv[1])==SQLITE_NULL;
  if( automatic && t->full ) return csError(t,SQLITE_FULL,"columnar automatic rowid exhausted");
  if( !automatic && sqlite3_value_type(argv[1])!=SQLITE_INTEGER ) return SQLITE_MISMATCH;
  id=automatic ? t->next : sqlite3_value_int64(argv[1]);
  for(i=0;i<t->ncol;i++){
    if( change && sqlite3_value_nochange(argv[i+2]) ) continue;
    changed|=(CsUnsigned)1<<i;
    rc=csFromValue(t,&incoming[i],argv[i+2],t->types[i]);
    if( rc ) goto done;
  }
  if( change && old==id ){
    rc=csIndexChange(t,id,incoming,changed);
    if( !rc ) rc=csPatchUpdate(t,id,incoming,changed);
    *rowid=id; goto done;
  }
  if( change ){
    rc=csWriteBlock(t,csBlockId(old));
    if( rc ) goto done;
    for(i=0;i<t->ncol;i++) if( !(changed&((CsUnsigned)1<<i)) ){
      rc=csCellCopy(&incoming[i],&t->write->cells[i][csSlot(old)]);
      if( rc ) goto done;
    }
  }
  rc=csWriteBlock(t,csBlockId(id));
  if( rc ) goto done;
  slot=csSlot(id); exists=csBit(t->write->live,slot);
  if( exists && (!change || old!=id) ){
    int conflict=sqlite3_vtab_on_conflict(t->db);
    /* The VM handles IGNORE and preserves changes() and last_insert_rowid(). */
    if( conflict!=SQLITE_REPLACE ){ rc=SQLITE_CONSTRAINT_ROWID; goto done; }
  }
  if( exists ){
    CsCell *values[CS_COLS];
    for(i=0;i<t->ncol;i++) values[i]=&t->write->cells[i][slot];
    rc=csIndexRow(t,id,values,1);
    if( rc ) goto done;
  }
  if( change && old!=id ){
    int oldSlot=csSlot(old);
    rc=csWriteBlock(t,csBlockId(old));
    if( rc ) goto done;
    if( csBit(t->write->live,oldSlot) ){
      CsCell *values[CS_COLS];
      for(i=0;i<t->ncol;i++) values[i]=&t->write->cells[i][oldSlot];
      rc=csIndexRow(t,old,values,1);
      if( rc ) goto done;
      csSet(t->write->live,oldSlot,0); t->write->count--; t->write->dirty=1;
      t->write->changed=((CsUnsigned)1<<t->ncol)-1;
      for(i=0;i<t->ncol;i++) csCellClear(&t->write->cells[i][oldSlot]);
    }
    rc=csWriteBlock(t,csBlockId(id));
    if( rc ) goto done;
  }
  if( !csBit(t->write->live,slot) ){ csSet(t->write->live,slot,1); t->write->count++; }
  {
    CsCell *values[CS_COLS];
    for(i=0;i<t->ncol;i++) values[i]=&incoming[i];
    rc=csIndexRow(t,id,values,0);
    if( rc ) goto done;
  }
  for(i=0;i<t->ncol;i++){
    csCellClear(&t->write->cells[i][slot]);
    t->write->cells[i][slot]=incoming[i]; memset(&incoming[i],0,sizeof(incoming[i]));
  }
  t->write->dirty=1; *rowid=id;
  t->write->changed=((CsUnsigned)1<<t->ncol)-1;
  if( id>=t->next ){
    if( id==INT64_MAX ) t->full=1; else t->next=id+1;
  }
done:
  for(i=0;i<t->ncol;i++) csCellClear(&incoming[i]);
  if( rc==SQLITE_CONSTRAINT_DATATYPE ) return csError(t,rc,"columnar columns require their declared INTEGER, REAL, TEXT or BLOB type, or NULL");
  if( rc==SQLITE_CONSTRAINT_ROWID ) return csError(t,rc,"duplicate columnar rowid");
  return rc;
}
static int csUpdate(sqlite3_vtab *v,int argc,sqlite3_value **argv,CsInt *rowid){
  CsTable *t=(CsTable*)v;
  int rc;
  t->busy++; rc=csUpdateBody(v,argc,argv,rowid); return csLeave(t,rc);
}

static int csBest(sqlite3_vtab *v,sqlite3_index_info *p){
  CsTable *t=(CsTable*)v;
  sqlite3_str *plan=sqlite3_str_new(t->db);
  int i,n=0,rc;
  for(i=0;i<p->nConstraint;i++){
    int col=p->aConstraint[i].iColumn,op=p->aConstraint[i].op;
    if( !p->aConstraint[i].usable || col>=t->ncol ) continue;
    if( op!=SQLITE_INDEX_CONSTRAINT_EQ && op!=SQLITE_INDEX_CONSTRAINT_GT
        && op!=SQLITE_INDEX_CONSTRAINT_GE && op!=SQLITE_INDEX_CONSTRAINT_LT
        && op!=SQLITE_INDEX_CONSTRAINT_LE ) continue;
    if( col>=0 && t->types[col]==SQLITE_TEXT
        && (!t->binaryUtf8 || sqlite3_stricmp(sqlite3_vtab_collation(p,i),"BINARY")) ) continue;
    p->aConstraintUsage[i].argvIndex=++n;
    /* Residual evaluation retains SQL affinity, NULL and collation semantics. */
    sqlite3_str_appendf(plan,"%d,%d;",col,op);
  }
  rc=sqlite3_str_errcode(plan);
  p->idxStr=sqlite3_str_finish(plan);
  if( rc ){ sqlite3_free(p->idxStr); p->idxStr=0; return rc; }
  if( !p->idxStr ){
    p->idxStr=sqlite3_mprintf("");
    if( !p->idxStr ) return SQLITE_NOMEM;
  }
  p->needToFreeIdxStr=1;
  p->estimatedCost=n ? 1000 : 1000000;
  p->estimatedRows=n ? CS_ROWS : 1000000;
  if( p->nOrderBy==1 && p->aOrderBy[0].iColumn<0 && !p->aOrderBy[0].desc ) p->orderByConsumed=1;
  rc=csIndexBest(t,p);
  return rc;
}
static int csOpen(sqlite3_vtab *v,sqlite3_vtab_cursor **out){
  CsCursor *c=(CsCursor*)sqlite3_malloc64(sizeof(*c));
  if( !c ) return SQLITE_NOMEM;
  memset(c,0,sizeof(*c)); c->base.pVtab=v; c->eof=1;
  *out=&c->base; return SQLITE_OK;
}
static void csResetCursor(CsCursor *c){
  CsTable *t=(CsTable*)c->base.pVtab;
  int i;
  for(i=0;i<c->nPred;i++) csCellClear(&c->pred[i].value);
  sqlite3_free(c->pred); c->pred=0; c->nPred=0; c->alwaysEmpty=0;
  sqlite3_finalize(c->blocks); sqlite3_finalize(c->column);
  sqlite3_finalize(c->indexScan); c->indexScan=0; c->indexCount=0;
  csFreeBlock(t,c->block); csFreeBlock(t,c->pending);
  c->blocks=0; c->column=0; c->block=0; c->pending=0;
  c->pos=CS_ROWS; c->eof=0; c->pendingDone=0; c->sqlReady=0;
  c->hasLower=c->hasUpper=c->strictLower=c->strictUpper=0;
}
static int csClose(sqlite3_vtab_cursor *v){
  CsCursor *c=(CsCursor*)v;
  csResetCursor(c); sqlite3_free(c); return SQLITE_OK;
}
static int csSelectBlock(CsCursor *c){
  CsTable *t=(CsTable*)c->base.pVtab;
  int i,j,rc=SQLITE_OK;
  if( !c->block ) return SQLITE_OK;
  memcpy(c->selected,c->block->live,CS_MAP);
  if( c->alwaysEmpty ){ memset(c->selected,0,CS_MAP); return SQLITE_OK; }
  for(j=0;j<c->nPred;j++){
    CsPredicate *p=&c->pred[j];
    rc=csLoadColumn(t,c->block,p->col,c->column);
    if( rc ) break;
    for(i=0;i<CS_ROWS;i++) if( csBit(c->selected,i) ){
      CsCell *cell=&c->block->cells[p->col][i];
      int cmp=cell->type ? csCellCmp(cell,&p->value) : 0;
      int match=cell->type && (p->op==SQLITE_INDEX_CONSTRAINT_EQ ? cmp==0
        : p->op==SQLITE_INDEX_CONSTRAINT_GT ? cmp>0 : p->op==SQLITE_INDEX_CONSTRAINT_GE ? cmp>=0
        : p->op==SQLITE_INDEX_CONSTRAINT_LT ? cmp<0 : cmp<=0);
      if( !match ){ csSet(c->selected,i,0); t->filteredRows++; }
    }
  }
  return rc;
}
static int csNextBlock(CsCursor *c){
  CsTable *t=(CsTable*)c->base.pVtab;
  int rc;
  csFreeBlock(t,c->block); c->block=0;
  if( !c->sqlReady ){
    rc=sqlite3_step(c->blocks);
    if( rc==SQLITE_ROW ) c->sqlReady=1;
    else if( rc==SQLITE_DONE ) c->sqlReady=2;
    else return rc;
  }
  if( c->pending && (c->sqlReady==2 || c->pending->id<sqlite3_column_int64(c->blocks,0)) ){
    c->block=c->pending; c->pending=0;
  }else if( c->sqlReady==1 ){
    c->block=csNewBlock(t,sqlite3_column_int64(c->blocks,0));
    if( !c->block ) return SQLITE_NOMEM;
    rc=csReadLive(t,c->block,c->blocks,1,2);
    if( rc ) return rc;
    t->blocksRead++; c->sqlReady=0;
  }else c->eof=1;
  c->pos=-1; return csSelectBlock(c);
}
static int csNext(sqlite3_vtab_cursor *v){
  CsCursor *c=(CsCursor*)v;
  CsTable *t=(CsTable*)v->pVtab;
  int rc;
  if( c->indexScan ){
    rc=sqlite3_step(c->indexScan);
    c->eof=rc!=SQLITE_ROW;
    if( !c->eof ){ t->rowsOut++; t->indexReads++; }
    return rc==SQLITE_ROW || rc==SQLITE_DONE ? SQLITE_OK : rc;
  }
  while( !c->eof ){
    if( c->block ){
      while( ++c->pos<CS_ROWS ){
        if( csBit(c->selected,c->pos) ){
          CsInt id=csRowid(c->block->id,c->pos);
          if( c->hasLower && (id<c->lower || (c->strictLower && id==c->lower)) ) continue;
          if( c->hasUpper && (id>c->upper || (c->strictUpper && id==c->upper)) ) continue;
          t->rowsOut++; return SQLITE_OK;
        }
      }
    }
    if( sqlite3_is_interrupted(t->db) ) return SQLITE_INTERRUPT;
    rc=csNextBlock(c);
    if( rc ) return rc;
  }
  return SQLITE_OK;
}
static int csFilter(sqlite3_vtab_cursor *v,int idx,const char *plan,int argc,sqlite3_value **argv){
  CsCursor *c=(CsCursor*)v;
  CsTable *t=(CsTable*)v->pVtab;
  sqlite3_str *sql;
  int i,rc,*bind,hasPending=t->write && t->write->dirty;
  (void)idx;
  csResetCursor(c);
  if( idx ) return csIndexFilter(c,idx,plan,argc,argv);
  c->pred=(CsPredicate*)sqlite3_malloc64(((CsUnsigned)argc+1)*sizeof(CsPredicate));
  if( !c->pred ) return SQLITE_NOMEM;
  memset(c->pred,0,((size_t)argc+1)*sizeof(CsPredicate));
  bind=(int*)sqlite3_malloc64(((CsUnsigned)argc+1)*sizeof(int));
  if( !bind ) return SQLITE_NOMEM;
  memset(bind,0,((size_t)argc+1)*sizeof(int));
  sql=sqlite3_str_new(t->db);
  sqlite3_str_appendf(sql,"SELECT b.block,b.live,b.n FROM \"%w\".\"%w_blocks\" b WHERE 1",t->schema,t->name);
  if( hasPending ){
    c->pending=csCopyBlock(t,t->write);
    if( !c->pending ){ sqlite3_free(sqlite3_str_finish(sql)); sqlite3_free(bind); return SQLITE_NOMEM; }
    sqlite3_str_appendf(sql," AND b.block!=?%d",argc+1);
  }
  for(i=0;i<argc;i++){
    char *end;
    int col=(int)strtol(plan,&end,10),op,kind=sqlite3_value_type(argv[i]);
    if( *end!=',' ){ sqlite3_free(sqlite3_str_finish(sql)); sqlite3_free(bind); return SQLITE_MISUSE; }
    op=(int)strtol(end+1,&end,10);
    if( *end!=';' ){ sqlite3_free(sqlite3_str_finish(sql)); sqlite3_free(bind); return SQLITE_MISUSE; }
    plan=end+1;
    if( kind==SQLITE_NULL ){ sqlite3_str_appendall(sql," AND 0"); c->alwaysEmpty=1; continue; }
    if( col<0 ){
      if( kind!=SQLITE_INTEGER ) continue;
      {
        CsInt value=sqlite3_value_int64(argv[i]);
        if( op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_GT || op==SQLITE_INDEX_CONSTRAINT_GE ){
          int strict=op==SQLITE_INDEX_CONSTRAINT_GT;
          if( !c->hasLower || value>c->lower ){ c->lower=value; c->strictLower=strict; }
          else if( value==c->lower ) c->strictLower|=strict;
          c->hasLower=1;
        }
        if( op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_LT || op==SQLITE_INDEX_CONSTRAINT_LE ){
          int strict=op==SQLITE_INDEX_CONSTRAINT_LT;
          if( !c->hasUpper || value<c->upper ){ c->upper=value; c->strictUpper=strict; }
          else if( value==c->upper ) c->strictUpper|=strict;
          c->hasUpper=1;
        }
      }
      sqlite3_str_appendf(sql," AND b.block%s?%d",
        op==SQLITE_INDEX_CONSTRAINT_EQ ? "=" : (op==SQLITE_INDEX_CONSTRAINT_GE || op==SQLITE_INDEX_CONSTRAINT_GT) ? ">=" : "<=",i+1);
      bind[i]=2;
    }else{
      if( col>=t->ncol ) continue;
      /* Canonical UTF-8 summaries and keys cannot prune UTF-16 BINARY text.
      ** Leave those predicates to the engine's encoding-aware collation. */
      if( t->types[col]==SQLITE_TEXT && !t->binaryUtf8 ) continue;
      if( t->types[col]==SQLITE_INTEGER || t->types[col]==SQLITE_FLOAT ){
        kind=sqlite3_value_numeric_type(argv[i]);
        if( kind!=SQLITE_INTEGER && kind!=SQLITE_FLOAT ) continue;
      }else if( kind!=t->types[col] ) continue;
      if( kind==t->types[col] ){
        CsPredicate *pred=&c->pred[c->nPred++];
        pred->col=col; pred->op=op;
        rc=csFromValue(t,&pred->value,argv[i],t->types[col]);
        if( rc ){ sqlite3_free(sqlite3_str_finish(sql)); sqlite3_free(bind); return rc; }
      }
      sqlite3_str_appendf(sql,
        " AND b.block IN (SELECT block FROM \"%w\".\"%w_columns\" WHERE col=%d AND nonnull>0 AND ",
        t->schema,t->name,col);
      if( op==SQLITE_INDEX_CONSTRAINT_EQ ) sqlite3_str_appendf(sql,"minimum<=?%d AND maximum>=?%d",i+1,i+1);
      else sqlite3_str_appendf(sql,"%s%s?%d",
        (op==SQLITE_INDEX_CONSTRAINT_GE || op==SQLITE_INDEX_CONSTRAINT_GT) ? "maximum" : "minimum",
        op==SQLITE_INDEX_CONSTRAINT_GT ? ">" : op==SQLITE_INDEX_CONSTRAINT_GE ? ">=" : op==SQLITE_INDEX_CONSTRAINT_LT ? "<" : "<=",i+1);
      sqlite3_str_appendf(sql," UNION SELECT block FROM \"%w\".\"%w_patch\" WHERE col=%d)",t->schema,t->name,col); bind[i]=1;
    }
  }
  sqlite3_str_appendall(sql," ORDER BY b.block");
  rc=csPrepare(t,&c->blocks,sqlite3_str_finish(sql));
  if( !rc ){
    for(i=0;i<argc && !rc;i++){
      if( bind[i]==2 ) rc=sqlite3_bind_int64(c->blocks,i+1,csBlockId(sqlite3_value_int64(argv[i])));
      else if( bind[i] ) rc=sqlite3_bind_value(c->blocks,i+1,argv[i]);
    }
    if( !rc && hasPending ) rc=sqlite3_bind_int64(c->blocks,argc+1,t->write->id);
  }
  sqlite3_free(bind);
  if( !rc ) rc=csColumnStatement(t,&c->column);
  if( !rc ) rc=csNext(v);
  return rc;
}
static int csEof(sqlite3_vtab_cursor *v){ return ((CsCursor*)v)->eof; }
static int csColumn(sqlite3_vtab_cursor *v,sqlite3_context *ctx,int col){
  CsCursor *c=(CsCursor*)v;
  CsTable *t=(CsTable*)v->pVtab;
  int rc;
  if( col==t->ncol ) return SQLITE_OK;
  if( col<0 || col>=t->ncol ) return SQLITE_MISUSE;
  if( sqlite3_vtab_nochange(ctx) ) return SQLITE_OK;
  if( c->indexScan ){
    int i;
    for(i=0;i<c->indexCount;i++) if( c->indexCols[i]==col ){
      sqlite3_result_value(ctx,sqlite3_column_value(c->indexScan,i+1)); return SQLITE_OK;
    }
    return SQLITE_MISUSE;
  }
  if( !c->block ) return SQLITE_MISUSE;
  rc=csLoadColumn(t,c->block,col,c->column);
  if( !rc ) csResult(ctx,&c->block->cells[col][c->pos]);
  return rc;
}
static int csRowidMethod(sqlite3_vtab_cursor *v,CsInt *id){
  CsCursor *c=(CsCursor*)v;
  *id=c->indexScan ? sqlite3_column_int64(c->indexScan,0) : csRowid(c->block->id,c->pos); return SQLITE_OK;
}
static void csRegistryRelease(void *p){
  CsRegistry *r=(CsRegistry*)p;
  if( --r->refs==0 ) sqlite3_free(r);
}
static int csDisconnect(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  CsTable **p=&t->registry->tables;
  int i;
  while( *p && *p!=t ) p=&(*p)->link;
  if( *p ) *p=t->link;
  csEnd(v);
  sqlite3_finalize(t->patchRead);
  for(i=0;i<t->ncol;i++) sqlite3_free(t->names[i]);
  sqlite3_free(t->schema); sqlite3_free(t->name);
  csRegistryRelease(t->registry); sqlite3_free(t);
  return SQLITE_OK;
}
static int csDeclaration(CsTable *t,int col,const char *decl){
  const char *p=decl;
  sqlite3_str *name=sqlite3_str_new(t->db);
  char quote=0;
  while( *p==' ' || *p=='\t' ) p++;
  if( *p=='"' || *p=='\'' || *p=='`' || *p=='[' ){ quote=*p=='[' ? ']' : *p; p++; }
  while( *p ){
    if( quote ){
      if( *p==quote ){
        p++;
        if( *p==quote ){ sqlite3_str_appendchar(name,1,*p++); continue; }
        break;
      }
    }else if( *p==' ' || *p=='\t' ) break;
    sqlite3_str_appendchar(name,1,*p++);
  }
  t->names[col]=sqlite3_str_finish(name);
  if( !t->names[col] || !t->names[col][0] ) return SQLITE_ERROR;
  while( *p==' ' || *p=='\t' ) p++;
  if( sqlite3_stricmp(p,"INTEGER")==0 || sqlite3_stricmp(p,"INT")==0 ) t->types[col]=SQLITE_INTEGER;
  else if( sqlite3_stricmp(p,"REAL")==0 ) t->types[col]=SQLITE_FLOAT;
  else if( sqlite3_stricmp(p,"TEXT")==0 ) t->types[col]=SQLITE_TEXT;
  else if( sqlite3_stricmp(p,"BLOB")==0 ) t->types[col]=SQLITE_BLOB;
  else return SQLITE_ERROR;
  return SQLITE_OK;
}
static int csConnect(sqlite3 *db,void *aux,int argc,const char *const*argv,sqlite3_vtab **out,char **error){
  CsTable *t;
  sqlite3_stmt *encoding=0;
  sqlite3_str *decl;
  char *sql;
  int i,rc=SQLITE_OK;
  if( argc<4 || argc>CS_COLS+3 ){
    *error=sqlite3_mprintf("sqlnew_columnar requires 1..63 typed columns"); return SQLITE_ERROR;
  }
  t=(CsTable*)sqlite3_malloc64(sizeof(*t));
  if( !t ) return SQLITE_NOMEM;
  memset(t,0,sizeof(*t)); t->db=db; t->registry=(CsRegistry*)aux; t->registry->refs++;
  t->schema=sqlite3_mprintf("%s",argv[1]); t->name=sqlite3_mprintf("%s",argv[2]); t->ncol=argc-3;
  if( !t->schema || !t->name ){ csDisconnect(&t->base); return SQLITE_NOMEM; }
  decl=sqlite3_str_new(db); sqlite3_str_appendall(decl,"CREATE TABLE x(");
  for(i=0;i<t->ncol && !rc;i++){
    rc=csDeclaration(t,i,argv[i+3]);
    if( !rc ) sqlite3_str_appendf(decl,"%s\"%w\" %s",i ? "," : "",t->names[i],
      t->types[i]==SQLITE_INTEGER ? "INTEGER" : t->types[i]==SQLITE_FLOAT ? "REAL" : t->types[i]==SQLITE_TEXT ? "TEXT" : "BLOB");
  }
  sqlite3_str_appendall(decl,",__sqlnew_command TEXT HIDDEN)"); sql=sqlite3_str_finish(decl);
  if( !sql ) rc=SQLITE_NOMEM;
  if( !rc ) rc=sqlite3_declare_vtab(db,sql);
  sqlite3_free(sql);
  if( !rc ) rc=csPrepare(t,&encoding,sqlite3_mprintf("PRAGMA \"%w\".encoding",t->schema));
  if( !rc ){
    rc=sqlite3_step(encoding);
    if( rc==SQLITE_ROW ){
      const char *enc=(const char*)sqlite3_column_text(encoding,0);
      if( !enc ) rc=SQLITE_NOMEM;
      else { t->binaryUtf8=sqlite3_stricmp(enc,"UTF-8")==0; rc=SQLITE_OK; }
    }else if( rc==SQLITE_DONE ) rc=SQLITE_ERROR;
  }
  sqlite3_finalize(encoding);
  if( rc ){
    *error=sqlite3_mprintf("column declarations must be name INTEGER, REAL, TEXT or BLOB");
    csDisconnect(&t->base); return rc;
  }
  sqlite3_vtab_config(db,SQLITE_VTAB_CONSTRAINT_SUPPORT,1);
  t->link=t->registry->tables; t->registry->tables=t;
  *out=&t->base; return SQLITE_OK;
}
static int csCreate(sqlite3 *db,void *aux,int argc,const char *const*argv,sqlite3_vtab **out,char **error){
  CsTable *t;
  int rc=csConnect(db,aux,argc,argv,out,error);
  if( rc ) return rc;
  t=(CsTable*)*out;
  rc=csExec(t,sqlite3_mprintf(
    "CREATE TABLE \"%w\".\"%w_meta\"(id INTEGER PRIMARY KEY,next INTEGER NOT NULL,full INTEGER NOT NULL);"
    "INSERT INTO \"%w\".\"%w_meta\" VALUES(1,1,0);"
    "CREATE TABLE \"%w\".\"%w_blocks\"(block INTEGER PRIMARY KEY,live BLOB NOT NULL,n INTEGER NOT NULL);"
    "CREATE TABLE \"%w\".\"%w_columns\"(block INTEGER,col INTEGER,minimum,maximum,"
    "nonnull INTEGER NOT NULL,sumlo INTEGER,sumhi INTEGER,lowlo INTEGER,lowhi INTEGER,highlo INTEGER,highhi INTEGER,"
    "PRIMARY KEY(block,col),UNIQUE(col,maximum,minimum,nonnull,block)) WITHOUT ROWID;"
    "CREATE TABLE \"%w\".\"%w_data\"(block INTEGER NOT NULL,col INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(block,col));"
    "CREATE TABLE \"%w\".\"%w_patch\"(col INTEGER,block INTEGER,slot INTEGER,value,original,PRIMARY KEY(col,block,slot)) WITHOUT ROWID;"
    "CREATE TABLE \"%w\".\"%w_indexes\"(id INTEGER PRIMARY KEY,name TEXT UNIQUE,definition TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)",
    t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name));
  if( !rc ) rc=csBegin(&t->base);
  if( rc ){ csDisconnect(*out); *out=0; }
  return rc;
}
static int csDestroy(sqlite3_vtab *v){
  CsTable *t=(CsTable*)v;
  int rc;
  sqlite3_finalize(t->patchRead); t->patchRead=0;
  rc=csIndexSchema(t,0);
  if( rc ) return rc;
  rc=csExec(t,sqlite3_mprintf(
    "DROP TABLE \"%w\".\"%w_columns\";DROP TABLE \"%w\".\"%w_data\";DROP TABLE \"%w\".\"%w_blocks\";DROP TABLE \"%w\".\"%w_patch\";DROP TABLE \"%w\".\"%w_indexes\";DROP TABLE \"%w\".\"%w_meta\"",
    t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name,t->schema,t->name));
  if( !rc ) csDisconnect(v);
  return rc;
}
static int csShadowName(const char *z){
  return !sqlite3_stricmp(z,"meta") || !sqlite3_stricmp(z,"blocks") || !sqlite3_stricmp(z,"columns") || !sqlite3_stricmp(z,"data") || !sqlite3_stricmp(z,"patch") || !sqlite3_stricmp(z,"indexes") || !sqlite3_strnicmp(z,"index_",6);
}
static int csRename(sqlite3_vtab *v,const char *name){
  CsTable *t=(CsTable*)v;
  char *copy=sqlite3_mprintf("%s",name);
  int rc;
  if( !copy ) return SQLITE_NOMEM;
  rc=csSync(v);
  sqlite3_finalize(t->patchRead); t->patchRead=0;
  if( !rc ) rc=csIndexSchema(t,name);
  if( !rc ) rc=csExec(t,sqlite3_mprintf(
    "ALTER TABLE \"%w\".\"%w_columns\" RENAME TO \"%w_columns\";"
    "ALTER TABLE \"%w\".\"%w_data\" RENAME TO \"%w_data\";"
    "ALTER TABLE \"%w\".\"%w_blocks\" RENAME TO \"%w_blocks\";"
    "ALTER TABLE \"%w\".\"%w_patch\" RENAME TO \"%w_patch\";"
    "ALTER TABLE \"%w\".\"%w_indexes\" RENAME TO \"%w_indexes\";"
    "ALTER TABLE \"%w\".\"%w_meta\" RENAME TO \"%w_meta\"",
    t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name,t->schema,t->name,name));
  if( !rc ){ sqlite3_free(t->name); t->name=copy; }else sqlite3_free(copy);
  return rc;
}
static int csIntegrity(sqlite3_vtab*,const char*,const char*,int,char**);
static sqlite3_module csModule={
  4,csCreate,csConnect,csBest,csDisconnect,csDestroy,csOpen,csClose,csFilter,csNext,csEof,csColumn,csRowidMethod,
  csUpdate,csBegin,csSync,csEnd,csEnd,0,csRename,csSavepoint,csRelease,csRollbackTo,csShadowName,csIntegrity
};
static CsTable *csFind(CsRegistry *r,const char *schema,const char *name){
  CsTable *t;
  if( !schema || !name ) return 0;
  for(t=r->tables;t;t=t->link){
    if( !sqlite3_stricmp(t->schema,schema) && !sqlite3_stricmp(t->name,name) ) return t;
  }
  return 0;
}
typedef struct CsAggregate CsAggregate;
#include "columnar_index.inc"
static int csCommand(CsTable *t,const char *command){
  if( !sqlite3_stricmp(command,"compact") ) return csCompact(t);
  if( command[0]=='{' ){
    sqlite3_stmt *s=0;
    char *name=0;
    int rc=sqlite3_prepare_v2(t->db,"SELECT json_extract(?,'$.drop_index')",-1,&s,0);
    if( !rc ){
      sqlite3_bind_text(s,1,command,-1,SQLITE_TRANSIENT); rc=sqlite3_step(s);
      if( rc==SQLITE_ROW ){
        const char *z=(const char*)sqlite3_column_text(s,0);
        if( z ) name=sqlite3_mprintf("%s",z);
        rc=z && !name ? SQLITE_NOMEM : SQLITE_OK;
      }
    }
    sqlite3_finalize(s);
    if( !rc ) rc=name ? csIndexDrop(t,name) : csIndexCreate(t,command);
    sqlite3_free(name); return rc;
  }
  return csError(t,SQLITE_ERROR,"unknown columnar command");
}
struct CsAggregate { CsInt count; CsWide sum; int overflow; CsCell best; };
static int csMergeSummary(CsAggregate *a,int op,CsStats *s){
  if( s->count<0 || a->count>INT64_MAX-s->count ) return SQLITE_TOOBIG;
  a->count+=s->count;
  if( !s->count ) return SQLITE_OK;
  if( op==2 ){
    if( !csFits(csAdd(a->sum,s->low)) || !csFits(csAdd(a->sum,s->high)) ) a->overflow=1;
    a->sum=csAdd(a->sum,s->sum);
  }else if( op==3 || op==4 ){
    CsCell *candidate=op==3 ? s->min : s->max;
    if( !candidate || !candidate->type ) return SQLITE_CORRUPT_VTAB;
    if( !a->best.type || (op==3 ? csCellCmp(candidate,&a->best)<0 : csCellCmp(candidate,&a->best)>0) ){
      csCellClear(&a->best);
      return csCellCopy(&a->best,candidate);
    }
  }
  return SQLITE_OK;
}
/* Called by the SQL compiler for supported aggregates over an entire table.
** The pointer identity is checked by select.c before generating that path. */
void sqlnew_columnar_summary(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsRegistry *r=(CsRegistry*)sqlite3_user_data(ctx);
  int op=sqlite3_value_int(argv[0]),col=sqlite3_value_int(argv[3]),rc=SQLITE_OK,pending;
  const char *schema=(const char*)sqlite3_value_text(argv[1]);
  const char *name=(const char*)sqlite3_value_text(argv[2]);
  CsTable *t=csFind(r,schema,name);
  sqlite3_stmt *s=0;
  CsAggregate a;
  (void)argc;
  memset(&a,0,sizeof(a));
  if( !t || op<0 || op>4 || (op!=0 && (col<0 || col>=t->ncol))
      || (op==2 && t->types[col]!=SQLITE_INTEGER) ){
    sqlite3_result_error(ctx,"unsupported columnar summary",-1); return;
  }
  if( !t->binaryUtf8 && (op==3 || op==4) && t->types[col]==SQLITE_TEXT ){
    /* Use the engine's text ordering. This query has no aggregate shortcut
    ** and includes pending writes through the ordinary virtual-table scan. */
    rc=csPrepare(t,&s,sqlite3_mprintf(
      "SELECT \"%w\" FROM \"%w\".\"%w\" WHERE \"%w\" IS NOT NULL ORDER BY \"%w\" %s LIMIT 1",
      t->names[col],schema,name,t->names[col],t->names[col],op==3 ? "ASC" : "DESC"));
    if( !rc ){
      rc=sqlite3_step(s);
      if( rc==SQLITE_ROW ){ sqlite3_result_value(ctx,sqlite3_column_value(s,0)); rc=SQLITE_OK; }
      else if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    }
    sqlite3_finalize(s);
    if( rc ) sqlite3_result_error_code(ctx,rc);
    return;
  }
  pending=t->write && t->write->dirty;
  if( op==0 ) rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT block,n FROM \"%w\".\"%w_blocks\" WHERE block!=? ORDER BY block",schema,name));
  else rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT block,nonnull,minimum,maximum,sumlo,sumhi,lowlo,lowhi,highlo,highhi,"
    "EXISTS(SELECT 1 FROM \"%w\".\"%w_patch\" p WHERE p.col=c.col AND p.block=c.block)"
    " FROM \"%w\".\"%w_columns\" c WHERE col=%d AND block!=? ORDER BY block",schema,name,schema,name,col));
  /* There is no valid group with this ID, even for INT64_MIN rowids. */
  if( !rc ) rc=sqlite3_bind_int64(s,1,pending ? t->write->id : INT64_MIN);
  while( !rc ){
    int step=sqlite3_step(s);
    if( step!=SQLITE_ROW && step!=SQLITE_DONE ){ rc=step; break; }
    if( pending && (step==SQLITE_DONE || t->write->id<sqlite3_column_int64(s,0)) ){
      CsStats stats;
      memset(&stats,0,sizeof(stats));
      if( op==0 ) stats.count=t->write->count;
      else stats=csDescribe(t->write,col);
      rc=csMergeSummary(&a,op,&stats); pending=0; t->summaryBlocks++;
      if( rc ) break;
    }
    if( step==SQLITE_DONE ) break;
    if( op!=0 && sqlite3_column_int(s,10) ){
      CsBlock *b=0;
      rc=csReadBlock(t,sqlite3_column_int64(s,0),(CsUnsigned)1<<col,&b);
      if( !rc ){
        CsStats stats=csDescribe(b,col);
        rc=csMergeSummary(&a,op,&stats); t->summaryBlocks++;
      }
      csFreeBlock(t,b);
      continue;
    }
    {
      CsStats stats;
      CsCell cells[2];
      memset(&stats,0,sizeof(stats)); memset(cells,0,sizeof(cells));
      stats.count=sqlite3_column_int(s,1);
      if( stats.count<0 || stats.count>CS_ROWS ){ rc=SQLITE_CORRUPT_VTAB; break; }
      if( op==2 ){
        stats.sum.lo=(CsUnsigned)sqlite3_column_int64(s,4); stats.sum.hi=sqlite3_column_int64(s,5);
        stats.low.lo=(CsUnsigned)sqlite3_column_int64(s,6); stats.low.hi=sqlite3_column_int64(s,7);
        stats.high.lo=(CsUnsigned)sqlite3_column_int64(s,8); stats.high.hi=sqlite3_column_int64(s,9);
      }else if( op==3 || op==4 ){
        rc=csFromValue(t,&cells[0],sqlite3_column_value(s,2),t->types[col]);
        if( !rc ) rc=csFromValue(t,&cells[1],sqlite3_column_value(s,3),t->types[col]);
        stats.min=&cells[0]; stats.max=&cells[1];
      }
      if( !rc ) rc=csMergeSummary(&a,op,&stats);
      csCellClear(&cells[0]); csCellClear(&cells[1]); t->summaryBlocks++;
    }
  }
  sqlite3_finalize(s);
  if( rc ) sqlite3_result_error_code(ctx,rc);
  else if( op==0 || op==1 ) sqlite3_result_int64(ctx,a.count);
  else if( op==2 ){
    if( a.overflow || !csFits(a.sum) ) sqlite3_result_error(ctx,"integer overflow",-1);
    else if( a.count ) sqlite3_result_int64(ctx,csSigned(a.sum.lo));
  }else csResult(ctx,&a.best);
  csCellClear(&a.best);
}
static int csIntegrity(sqlite3_vtab *v,const char *schema,const char *name,int quick,char **error){
  CsTable *t=(CsTable*)v;
  sqlite3_vtab_cursor *cursor=0;
  CsIndex *indexes=0,*index;
  CsInt total=0;
  CsCursor *c;
  sqlite3_stmt *meta=0,*orphans=0;
  CsInt block=0;
  int rc,col=0,i;
  (void)quick;
  rc=csOpen(v,&cursor);
  if( rc ) return rc;
  c=(CsCursor*)cursor;
  rc=csFilter(cursor,0,"",0,0);
  if( !rc ) rc=csIndexesLoad(t,&indexes);
  if( !rc ) rc=csPrepare(t,&meta,sqlite3_mprintf(
    "SELECT nonnull,minimum,maximum,sumlo,sumhi,lowlo,lowhi,highlo,highhi FROM \"%w\".\"%w_columns\" WHERE block=? AND col=?",
    schema,name));
  while( !rc && !c->eof ){
    int pending=t->write && t->write->dirty && c->block->id==t->write->id;
    block=c->block->id;
    for(col=0;!rc && col<t->ncol;col++){
      CsStats stats;
      CsCell values[2];
      CsBlock *base=0;
      CsUnsigned patches=t->patchesRead;
      rc=csLoadColumn(t,c->block,col,c->column);
      if( rc ) break;
      for(i=0;i<CS_ROWS;i++){
        if( !csBit(c->block->live,i) && c->block->cells[col][i].type ){ rc=SQLITE_CORRUPT_VTAB; break; }
      }
      if( rc || pending ) continue;
      if( patches!=t->patchesRead ) rc=csBaseColumn(t,c->block,col,&base);
      if( rc ) break;
      stats=csDescribe(base ? base : c->block,col); memset(values,0,sizeof(values));
      sqlite3_bind_int64(meta,1,block); sqlite3_bind_int(meta,2,col);
      rc=sqlite3_step(meta);
      if( rc==SQLITE_ROW ){
        rc=csFromValue(t,&values[0],sqlite3_column_value(meta,1),t->types[col]);
        if( !rc ) rc=csFromValue(t,&values[1],sqlite3_column_value(meta,2),t->types[col]);
        if( !rc && (sqlite3_column_int(meta,0)!=stats.count
          || (stats.count && (!values[0].type || !values[1].type
              || csCellCmp(stats.min,&values[0]) || csCellCmp(stats.max,&values[1])))
          || (!stats.count && (values[0].type || values[1].type))
          || sqlite3_column_int64(meta,3)!=csSigned(stats.sum.lo) || sqlite3_column_int64(meta,4)!=stats.sum.hi
          || sqlite3_column_int64(meta,5)!=csSigned(stats.low.lo) || sqlite3_column_int64(meta,6)!=stats.low.hi
          || sqlite3_column_int64(meta,7)!=csSigned(stats.high.lo) || sqlite3_column_int64(meta,8)!=stats.high.hi)
        ) rc=SQLITE_CORRUPT_VTAB;
      }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
      csCellClear(&values[0]); csCellClear(&values[1]); sqlite3_reset(meta);
      csFreeBlock(t,base);
    }
    for(i=0;!rc && i<CS_ROWS;i++) if( csBit(c->block->live,i) ){
      CsCell *values[CS_COLS];
      int j;
      total++;
      for(j=0;j<t->ncol;j++) values[j]=&c->block->cells[j][i];
      for(index=indexes;!rc && index;index=index->next) rc=csIndexVerify(t,index,csRowid(block,i),values);
    }
    if( !rc ){ c->pos=CS_ROWS; rc=csNext(cursor); }
  }
  sqlite3_finalize(meta); csClose(cursor);
  for(index=indexes;!rc && index;index=index->next){
    sqlite3_stmt *count=0;
    rc=csPrepare(t,&count,sqlite3_mprintf("SELECT count(*) FROM \"%w\".\"%w_index_%d\"",schema,name,index->id));
    if( !rc ){
      rc=sqlite3_step(count);
      if( rc==SQLITE_ROW ) rc=sqlite3_column_int64(count,0)==total ? SQLITE_OK : SQLITE_CORRUPT_VTAB;
    }
    sqlite3_finalize(count);
  }
  csIndexesFree(indexes);
  if( !rc ) rc=csPrepare(t,&orphans,sqlite3_mprintf(
    "SELECT 1 FROM \"%w\".\"%w_columns\" c LEFT JOIN \"%w\".\"%w_blocks\" b USING(block)"
    " WHERE b.block IS NULL OR c.col<0 OR c.col>=%d"
    " UNION ALL SELECT 1 FROM \"%w\".\"%w_data\" d LEFT JOIN \"%w\".\"%w_columns\" c USING(block,col)"
    " WHERE c.col IS NULL"
    " UNION ALL SELECT 1 FROM \"%w\".\"%w_patch\" p LEFT JOIN \"%w\".\"%w_data\" d USING(block,col)"
    " WHERE d.col IS NULL OR p.slot<0 OR p.slot>=1024 LIMIT 1",
    schema,name,schema,name,t->ncol,schema,name,schema,name,schema,name,schema,name));
  if( !rc ){
    rc=sqlite3_step(orphans);
    rc=rc==SQLITE_DONE ? SQLITE_OK : rc==SQLITE_ROW ? SQLITE_CORRUPT_VTAB : rc;
  }
  sqlite3_finalize(orphans);
  if( rc && rc!=SQLITE_NOMEM && rc!=SQLITE_INTERRUPT ){
    *error=sqlite3_mprintf("In columnar %s.%s: invalid block %lld, column %d or summary metadata",schema,name,block,col);
    return *error ? SQLITE_OK : SQLITE_NOMEM;
  }
  return rc;
}
#include "columnar_batch.inc"
static void csStatistics(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsRegistry *r=(CsRegistry*)sqlite3_user_data(ctx);
  const char *name=(const char*)sqlite3_value_text(argv[0]);
  CsTable *t=csFind(r,"main",name);
  char *json;
  (void)argc;
  if( !t ){ sqlite3_result_error(ctx,"columnar table is not connected in main",-1); return; }
  json=sqlite3_mprintf(
    "{\"blocks_read\":%llu,\"columns_decoded\":%llu,\"compressed_bytes_read\":%llu,"
    "\"rows_emitted\":%llu,\"summary_blocks\":%llu,\"blocks_written\":%llu,"
    "\"columns_written\":%llu,\"patches_written\":%llu,\"patches_read\":%llu,\"compactions\":%llu,"
    "\"rows_filtered\":%llu,\"batch_rows\":%llu,\"index_rows_read\":%llu,\"index_rows_written\":%llu,\"batch_fallbacks\":%llu,"
    "\"connection_payload_bytes\":%llu,\"connection_payload_peak\":%llu,\"connection_payload_limit\":%llu}",
    t->blocksRead,t->columnsRead,t->bytesRead,t->rowsOut,t->summaryBlocks,t->blocksWritten,
    t->columnsWritten,t->patchesWritten,t->patchesRead,t->compactions,t->filteredRows,t->batchRows,
    t->indexReads,t->indexWrites,t->batchFallbacks,t->registry->bytes,t->registry->peakBytes,t->registry->byteLimit);
  if( !json ) sqlite3_result_error_nomem(ctx);
  else sqlite3_result_text(ctx,json,-1,sqlite3_free);
}
static void csMemoryLimit(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsRegistry *r=(CsRegistry*)sqlite3_user_data(ctx);
  CsUnsigned previous=r->byteLimit;
  if( argc>1 || (argc==1 && sqlite3_value_type(argv[0])!=SQLITE_INTEGER) ){
    sqlite3_result_error(ctx,"expected zero arguments or a positive byte limit",-1); return;
  }
  if( argc==1 ){
    CsInt limit=sqlite3_value_int64(argv[0]);
    if( limit<=0 ){ sqlite3_result_error(ctx,"memory limit must be positive",-1); return; }
    r->byteLimit=(CsUnsigned)limit;
  }
  sqlite3_result_int64(ctx,(CsInt)previous);
}
static void csFolding(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsRegistry *r=(CsRegistry*)sqlite3_user_data(ctx);
  int previous=r->folding;
  if( argc>1 || (argc==1 && (sqlite3_value_type(argv[0])!=SQLITE_INTEGER
      || sqlite3_value_int64(argv[0])<0 || sqlite3_value_int64(argv[0])>1)) ){
    sqlite3_result_error(ctx,"expected zero arguments or folding mode 0 or 1",-1); return;
  }
  if( argc==1 ) r->folding=sqlite3_value_int(argv[0]);
  sqlite3_result_int(ctx,previous);
}
int sqlnew_columnar_init(sqlite3 *db){
  CsRegistry *r=(CsRegistry*)sqlite3_malloc64(sizeof(*r));
  int rc;
  if( !r ) return SQLITE_NOMEM;
  memset(r,0,sizeof(*r)); r->refs=1; r->byteLimit=64*1024*1024;
  rc=sqlite3_create_module_v2(db,"sqlnew_columnar",&csModule,r,csRegistryRelease);
  if( rc ) return rc;
  r->refs++;
  rc=sqlite3_create_function_v2(db,"sqlnew_columnar_stats",1,SQLITE_UTF8,r,csStatistics,0,0,csRegistryRelease);
  if( !rc ){
    r->refs++;
    rc=sqlite3_create_function_v2(db,"sqlnew_columnar_memory_limit",-1,SQLITE_UTF8|SQLITE_DIRECTONLY,r,csMemoryLimit,0,0,csRegistryRelease);
  }
  if( !rc ){
    r->refs++;
    rc=sqlite3_create_function_v2(db,"sqlnew_columnar_folding",-1,SQLITE_UTF8|SQLITE_DIRECTONLY,r,csFolding,0,0,csRegistryRelease);
  }
  if( !rc ){
    r->refs++;
    rc=sqlite3_create_function_v2(db,"sqlnew_internal_summary",4,SQLITE_UTF8,r,sqlnew_columnar_summary,0,0,csRegistryRelease);
  }
  if( !rc ){
    r->refs++;
    rc=sqlite3_create_function_v2(db,"sqlnew_internal_batch",-1,SQLITE_UTF8,r,sqlnew_columnar_batch,0,0,csRegistryRelease);
  }
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_internal_batch_next",1,SQLITE_UTF8,0,sqlnew_columnar_batch_next,0,0);
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_internal_batch_value",2,SQLITE_UTF8,0,sqlnew_columnar_batch_value,0,0);
  return rc;
}
