/*
** SQLNEW block compression and vector execution.
** All persistent integers and IEEE-754 binary32 values are little-endian.
** See sqlnew/FORMAT.md for the byte format and limits.
*/
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT1
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#if FLT_RADIX!=2 || FLT_MANT_DIG!=24
# error SQLNEW requires IEEE-754 binary32 float
#endif
typedef char SnFloatMustBeFourBytes[sizeof(float)==4 ? 1 : -1];

#if !defined(SQLNEW_DISABLE_SIMD) && (defined(_M_X64) || defined(__x86_64__) || defined(__i386__))
# define SN_X86 1
# include <immintrin.h>
# ifdef _MSC_VER
#  include <intrin.h>
#  define SN_AVX __declspec(noinline)
# else
#  include <cpuid.h>
#  define SN_AVX __attribute__((target("avx2"),noinline))
# endif
#else
# define SN_X86 0
#endif

#define SN_MAX_BYTES (64*1024*1024)
#define SN_MAX_DIM 4096
#define SN_MAX_K 65536
#define SN_HEADER 16
int sqlnew_int_init(sqlite3 *db);
int sqlnew_columnar_init(sqlite3 *db);
int sqlnew_fold_init(sqlite3 *db);
int sqlnew_region_init(sqlite3 *db);
#ifndef SQLNEW_VERSION
# define SQLNEW_VERSION "0.2.1-dev"
#endif
typedef unsigned char SnByte;
typedef uint32_t SnU32;
typedef sqlite3_uint64 SnU64;

static SnU32 snGet32(const SnByte *p){
  return (SnU32)p[0] | ((SnU32)p[1]<<8) | ((SnU32)p[2]<<16)
       | ((SnU32)p[3]<<24);
}
static void snPut32(SnByte *p, SnU32 v){
  p[0]=(SnByte)v; p[1]=(SnByte)(v>>8);
  p[2]=(SnByte)(v>>16); p[3]=(SnByte)(v>>24);
}
static sqlite3_int64 snGetId(const SnByte *p){
  SnU64 u=(SnU64)snGet32(p)|((SnU64)snGet32(p+4)<<32);
  sqlite3_int64 v;
  memcpy(&v,&u,8);
  return v;
}
static void snPutId(SnByte *p, sqlite3_int64 v){
  SnU64 u=(SnU64)v;
  snPut32(p,(SnU32)u); snPut32(p+4,(SnU32)(u>>32));
}
static float snGetFloat(const SnByte *p){
  SnU32 u=snGet32(p);
  float f;
  memcpy(&f,&u,4);
  return f;
}
static void snPutFloat(SnByte *p, float f){
  SnU32 u;
  memcpy(&u,&f,4); snPut32(p,u);
}
static int snFinite(float f){ return f<=FLT_MAX && f>=-FLT_MAX; }
static void snError(sqlite3_context *c, const char *s){
  sqlite3_result_error(c,s,-1);
}
static int snLimit(sqlite3 *db){
  int n=sqlite3_limit(db,SQLITE_LIMIT_LENGTH,-1);
  return n<SN_MAX_BYTES ? n : SN_MAX_BYTES;
}
static SnU32 snChecksum(const SnByte *p, int n){
  SnU32 a=1,b=0;
  while( n>0 ){
    int m=n>4096 ? 4096 : n;
    n-=m;
    while( m-- ){ a+=*p++; b+=a; }
    a%=65521; b%=65521;
  }
  return (b<<16)|a;
}
static int snLiteral(SnByte *out, int w, const SnByte *in, int n){
  while( n ){
    int m=n>128 ? 128 : n;
    out[w++]=(SnByte)(m-1);
    memcpy(out+w,in,(size_t)m); w+=m; in+=m; n-=m;
  }
  return w;
}

/* Greedy hash match finder, bounded to a 65535-byte back-reference window.
** Raw fallback includes its header and avoids expanding incompressible data
** beyond the fixed 16-byte frame overhead. */
int sqlnew_pack_buffer_limit(sqlite3 *db,const void *input,int n,int stop,void **result,int *size){
  const SnByte *in=(const SnByte*)input;
  SnByte *out;
  int *table;
  int i=0,start=0,w=SN_HEADER,limit;
  SnU64 capacity;
  *result=0; *size=0;
  if( !in && n ) return SQLITE_NOMEM;
  limit=snLimit(db);
  if( limit<SN_HEADER || n>limit-SN_HEADER ){
    return SQLITE_TOOBIG;
  }
  capacity=(SnU64)n+(SnU64)n/128+SN_HEADER+2;
  out=(SnByte*)sqlite3_malloc64(capacity);
  table=(int*)sqlite3_malloc64(65536*sizeof(int));
  if( !out || !table ){
    sqlite3_free(out); sqlite3_free(table);
    return SQLITE_NOMEM;
  }
  for(i=0;i<65536;i++) table[i]=-1;
  i=0;
  while( i+4<=n ){
    SnU32 h=(snGet32(in+i)*2654435761u)>>16;
    int prev=table[h],len=0;
    table[h]=i;
    if( prev>=0 && i-prev<=65535 && memcmp(in+prev,in+i,4)==0 ){
      len=4;
      while( len<131 && i+len<n && in[prev+len]==in[i+len] ) len++;
    }
    if( len ){
      int dist=i-prev;
      w=snLiteral(out,w,in+start,i-start);
      out[w++]=(SnByte)(0x80|(len-4));
      out[w++]=(SnByte)dist; out[w++]=(SnByte)(dist>>8);
      i+=len; start=i;
    }else{
      i++;
    }
    /* Already-written output and pending literals cannot be removed. Stop
    ** when both coded and raw choices are proven too large for a competitor. */
    if( stop && n+SN_HEADER>=stop && w+i-start>=stop ){
      sqlite3_free(out); sqlite3_free(table); return SQLITE_DONE;
    }
  }
  if( n>start ) w=snLiteral(out,w,in+start,n-start);
  sqlite3_free(table);
  memcpy(out,"SNZ1",4); snPut32(out+4,(SnU32)n);
  snPut32(out+8,snChecksum(in,n)); memset(out+12,0,4);
  if( w>=n+SN_HEADER ){
    if( n ) memcpy(out+SN_HEADER,in,(size_t)n);
    w=n+SN_HEADER;
  }else{
    out[12]=1;
  }
  assert((SnU64)w<=capacity);
  *result=out; *size=w; return SQLITE_OK;
}
int sqlnew_pack_buffer(sqlite3 *db,const void *input,int n,void **result,int *size){
  return sqlnew_pack_buffer_limit(db,input,n,0,result,size);
}
static void snPack(sqlite3_context *c, int argc, sqlite3_value **argv){
  void *out=0;
  int size=0,rc;
  (void)argc;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return;
  rc=sqlnew_pack_buffer(sqlite3_context_db_handle(c),sqlite3_value_blob(argv[0]),
      sqlite3_value_bytes(argv[0]),&out,&size);
  if( rc ) sqlite3_result_error_code(c,rc);
  else sqlite3_result_blob(c,out,size,sqlite3_free);
}
int sqlnew_unpack_buffer(sqlite3 *db,const void *input,int n,void **result,int *bytes){
  const SnByte *in=(const SnByte*)input;
  SnByte *out;
  SnU32 declared;
  int w=0,r=SN_HEADER,size;
  *result=0; *bytes=0;
  if( !in || n<SN_HEADER || memcmp(in,"SNZ1",4) || in[12]>1
      || in[13] || in[14] || in[15] ){
    return SQLITE_CORRUPT;
  }
  declared=snGet32(in+4);
  if( declared>(SnU32)snLimit(db) ){
    return SQLITE_TOOBIG;
  }
  size=(int)declared;
  /* Even the longest match emits at most 131 bytes per three-byte token. */
  if( (in[12]==0 && n-SN_HEADER!=size)
      || (in[12]==1 && (SnU64)size>(SnU64)(n-SN_HEADER)*44) ){
    return SQLITE_CORRUPT;
  }
  out=(SnByte*)sqlite3_malloc64((SnU64)size+1);
  if( !out ) return SQLITE_NOMEM;
  if( in[12]==0 ){
    if( size ) memcpy(out,in+SN_HEADER,(size_t)size);
    w=size; r=n;
  }else{
    while( r<n ){
      unsigned token=in[r++];
      int len;
      if( token<128 ){
        len=(int)token+1;
        if( len>n-r || len>size-w ) goto bad;
        memcpy(out+w,in+r,(size_t)len); r+=len; w+=len;
      }else{
        int dist,j;
        len=(int)(token&127)+4;
        if( n-r<2 || len>size-w ) goto bad;
        dist=in[r]|((int)in[r+1]<<8); r+=2;
        if( dist==0 || dist>w ) goto bad;
        for(j=0;j<len;j++){ out[w]=out[w-dist]; w++; }
      }
    }
  }
  if( r!=n || w!=size || snChecksum(out,w)!=snGet32(in+8) ) goto bad;
  out[w]=0; *result=out; *bytes=w; return SQLITE_OK;
bad:
  sqlite3_free(out); return SQLITE_CORRUPT;
}
static void snUnpack(sqlite3_context *c,int argc,sqlite3_value **argv){
  void *out=0;
  int n=0,rc;
  (void)argc;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return;
  rc=sqlnew_unpack_buffer(sqlite3_context_db_handle(c),sqlite3_value_blob(argv[0]),
      sqlite3_value_bytes(argv[0]),&out,&n);
  if( rc ) sqlite3_result_error_code(c,rc);
  else sqlite3_result_blob(c,out,n,sqlite3_free);
}

typedef double (*SnDistance)(const SnByte*,const float*,int);
typedef double (*SnQuantDistance)(const SnByte*,float,const float*,int);
typedef struct SnBackend SnBackend;
struct SnBackend { SnDistance distance; SnQuantDistance quant; const char *name; };
static double snL2Scalar(const SnByte *p, const float *q, int d){
  double total=0;
  int i;
  for(i=0;i<d;i++){
    float f=snGetFloat(p+4*i);
    double z;
    if( !snFinite(f) ) return -1;
    z=(double)f-(double)q[i]; total+=z*z;
  }
  return total;
}
static double snQ8Scalar(const SnByte *p,float scale,const float *q,int d){
  double total=0;
  int i;
  for(i=0;i<d;i++){
    int code=p[i]<128 ? p[i] : (int)p[i]-256;
    double z;
    if( code== -128 ) return -1;
    z=(double)code*scale-(double)q[i]; total+=z*z;
  }
  return total;
}
#if SN_X86
SN_AVX static double snL2Avx(const SnByte *p, const float *q, int d){
  __m256d sum=_mm256_setzero_pd();
  double lane[4],total;
  int i=0;
  __m128i exponent=_mm_set1_epi32(0x7f800000);
  for(;i+4<=d;i+=4){
    __m128 x=_mm_loadu_ps((const float*)(const void*)(p+4*i));
    __m128 y=_mm_loadu_ps(q+i);
    __m128i bits=_mm_and_si128(_mm_castps_si128(x),exponent);
    __m256d z=_mm256_sub_pd(_mm256_cvtps_pd(x),_mm256_cvtps_pd(y));
    if( _mm_movemask_epi8(_mm_cmpeq_epi32(bits,exponent)) ) return -1;
    sum=_mm256_add_pd(sum,_mm256_mul_pd(z,z));
  }
  _mm256_storeu_pd(lane,sum);
  total=(lane[0]+lane[1])+(lane[2]+lane[3]);
  for(;i<d;i++){
    float f=snGetFloat(p+4*i);
    double z;
    if( !snFinite(f) ) return -1;
    z=(double)f-(double)q[i]; total+=z*z;
  }
  return total;
}
SN_AVX static double snQ8Avx(const SnByte *p,float scale,const float *q,int d){
  __m256d sum=_mm256_setzero_pd(),s=_mm256_set1_pd((double)scale);
  double lanes[4],total;
  int i=0;
  for(;i+4<=d;i+=4){
    int raw;
    __m128i codes;
    __m256d z;
    memcpy(&raw,p+i,4);
    codes=_mm_cvtepi8_epi32(_mm_cvtsi32_si128(raw));
    if( _mm_movemask_epi8(_mm_cmpeq_epi32(codes,_mm_set1_epi32(-128))) ) return -1;
    z=_mm256_sub_pd(_mm256_mul_pd(_mm256_cvtepi32_pd(codes),s),_mm256_cvtps_pd(_mm_loadu_ps(q+i)));
    sum=_mm256_add_pd(sum,_mm256_mul_pd(z,z));
  }
  _mm256_storeu_pd(lanes,sum);
  total=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
  for(;i<d;i++){
    int code=p[i]<128 ? p[i] : (int)p[i]-256;
    double z;
    if( code== -128 ) return -1;
    z=(double)code*scale-(double)q[i]; total+=z*z;
  }
  return total;
}
static int snHasAvx2(void){
# ifdef _MSC_VER
  int a[4];
  __cpuid(a,0); if( a[0]<7 ) return 0;
  __cpuid(a,1);
  if( (a[2]&(1<<27))==0 || (a[2]&(1<<28))==0 ) return 0;
  if( (_xgetbv(0)&6)!=6 ) return 0;
  __cpuidex(a,7,0); return (a[1]&(1<<5))!=0;
# else
  __builtin_cpu_init();
  return __builtin_cpu_supports("avx2")!=0;
# endif
}
#endif
static const SnBackend snScalar={snL2Scalar,snQ8Scalar,"scalar-f64"};
#if SN_X86
static const SnBackend snAvx={snL2Avx,snQ8Avx,"avx2-f64"};
#endif
static int snReadVector(sqlite3_value *v, float *out){
  const SnByte *p;
  int n,i;
  if( sqlite3_value_type(v)!=SQLITE_BLOB ) return 0;
  n=sqlite3_value_bytes(v);
  if( n==0 || n%4 || n/4>SN_MAX_DIM ) return 0;
  p=(const SnByte*)sqlite3_value_blob(v);
  if( !p ) return 0;
  for(i=0;i<n/4;i++){
    float f=snGetFloat(p+4*i);
    if( !snFinite(f) ) return 0;
    if( out ) out[i]=f;
  }
  return n/4;
}
static void snL2(sqlite3_context *c,int argc,sqlite3_value **argv){
  float q[SN_MAX_DIM];
  int d=snReadVector(argv[1],q);
  const SnBackend *b=(const SnBackend*)sqlite3_user_data(c);
  (void)argc;
  if( !d || snReadVector(argv[0],0)!=d ){
    snError(c,"vectors must be finite little-endian float32 blobs of equal dimension (1..4096)"); return;
  }
  sqlite3_result_double(c,b->distance((const SnByte*)sqlite3_value_blob(argv[0]),q,d));
}
static void snBackendName(sqlite3_context *c,int argc,sqlite3_value **argv){
  const SnBackend *b=(const SnBackend*)sqlite3_user_data(c);
  (void)argc; (void)argv;
  sqlite3_result_text(c,b->name,-1,SQLITE_STATIC);
}
static void snVersion(sqlite3_context *c,int argc,sqlite3_value **argv){
  (void)argc; (void)argv;
  sqlite3_result_text(c,SQLNEW_VERSION,-1,SQLITE_STATIC);
}

typedef struct SnBuilder SnBuilder;
struct SnBuilder { SnByte *p; int size,cap,dim,mode,count,failed; };
static void snBlockStep(sqlite3_context *c,int argc,sqlite3_value **argv){
  SnBuilder *b=(SnBuilder*)sqlite3_aggregate_context(c,sizeof(*b));
  float values[SN_MAX_DIM],maximum=0,scale=0;
  int d,mode,stride,need,i;
  SnByte *p;
  (void)argc;
  if( !b ){ sqlite3_result_error_nomem(c); return; }
  if( b->failed ) return;
  d=snReadVector(argv[1],values);
  mode=sqlite3_value_int(argv[2]);
  if( !d || sqlite3_value_type(argv[0])!=SQLITE_INTEGER
      || sqlite3_value_type(argv[2])!=SQLITE_INTEGER || (mode!=0 && mode!=1)
      || sqlite3_value_int64(argv[2])!=(sqlite3_int64)mode
      || (b->count && (d!=b->dim || mode!=b->mode)) ){
    b->failed=1;
    snError(c,"vector_block requires integer ID, consistent finite float32 vectors, and mode 0 or 1"); return;
  }
  stride=8+(mode ? d+4 : 4*d);
  if( !b->size ) b->size=SN_HEADER;
  if( stride>snLimit(sqlite3_context_db_handle(c))-b->size ){
    b->failed=1; sqlite3_result_error_toobig(c); return;
  }
  need=b->size+stride;
  if( need>b->cap ){
    int cap=need<32768 ? 32768 : need;
    if( b->cap && b->cap<=SN_MAX_BYTES/2 && b->cap*2>cap ) cap=b->cap*2;
    p=(SnByte*)sqlite3_realloc64(b->p,(SnU64)cap);
    if( !p ){ b->failed=1; sqlite3_result_error_nomem(c); return; }
    b->p=p; b->cap=cap;
  }
  b->dim=d; b->mode=mode;
  p=b->p+b->size; snPutId(p,sqlite3_value_int64(argv[0])); p+=8;
  if( mode ){
    for(i=0;i<d;i++) if( fabsf(values[i])>maximum ) maximum=fabsf(values[i]);
    scale=maximum/127.0f;
    if( maximum>0 && scale==0 ) scale=maximum;
    snPutFloat(p,scale); p+=4;
    for(i=0;i<d;i++){
      double v=scale ? (double)values[i]/scale : 0;
      int code=(int)(v>=0 ? floor(v+0.5) : ceil(v-0.5));
      if( code>127 ) code=127;
      if( code< -127 ) code= -127;
      p[i]=(SnByte)(code&255);
    }
  }else{
    memcpy(p,sqlite3_value_blob(argv[1]),(size_t)4*d);
  }
  b->size=need; b->count++;
}
static void snBlockFinal(sqlite3_context *c){
  SnBuilder *b=(SnBuilder*)sqlite3_aggregate_context(c,0);
  if( !b ) return;
  if( b->failed ){ sqlite3_free(b->p); b->p=0; return; }
  memcpy(b->p,"SNV1",4); snPut32(b->p+4,(SnU32)b->dim);
  snPut32(b->p+8,(SnU32)b->count); snPut32(b->p+12,(SnU32)b->mode);
  sqlite3_result_blob(c,b->p,b->size,sqlite3_free); b->p=0;
}

typedef struct SnHit SnHit;
struct SnHit { sqlite3_int64 id; double distance; };
typedef struct SnTable SnTable;
struct SnTable { sqlite3_vtab base; sqlite3 *db; const SnBackend *backend; };
typedef struct SnCursor SnCursor;
struct SnCursor {
  sqlite3_vtab_cursor base;
  SnHit *hits;
  sqlite3_value *parameters[5];
  int count,pos;
};
/* Largest (distance, ID) is the worst retained result and lives at heap root. */
static int snWorse(const SnHit *a,const SnHit *b){
  return a->distance>b->distance || (a->distance==b->distance && a->id>b->id);
}
static int snHitCompare(const void *a,const void *b){
  const SnHit *x=(const SnHit*)a,*y=(const SnHit*)b;
  return snWorse(x,y) ? 1 : snWorse(y,x) ? -1 : 0;
}
static void snHeapOffer(SnCursor *c,SnHit h,int k){
  int i,j;
  if( c->count<k ){
    i=c->count++;
    while( i>0 ){
      j=(i-1)/2;
      if( !snWorse(&h,&c->hits[j]) ) break;
      c->hits[i]=c->hits[j]; i=j;
    }
    c->hits[i]=h;
  }else if( snWorse(&c->hits[0],&h) ){
    i=0;
    while( (j=2*i+1)<c->count ){
      if( j+1<c->count && snWorse(&c->hits[j+1],&c->hits[j]) ) j++;
      if( !snWorse(&c->hits[j],&h) ) break;
      c->hits[i]=c->hits[j]; i=j;
    }
    c->hits[i]=h;
  }
}
static int snConnect(sqlite3 *db,void *aux,int argc,const char *const*argv,
                     sqlite3_vtab **out,char **err){
  SnTable *t;
  int rc;
  (void)argc; (void)argv; (void)err;
  rc=sqlite3_declare_vtab(db,"CREATE TABLE x(id INTEGER,distance REAL,block HIDDEN,query HIDDEN,k HIDDEN,excluded HIDDEN,included HIDDEN)");
  if( rc!=SQLITE_OK ) return rc;
  t=(SnTable*)sqlite3_malloc64(sizeof(*t));
  if( !t ) return SQLITE_NOMEM;
  memset(t,0,sizeof(*t)); t->db=db; t->backend=(const SnBackend*)aux;
  *out=&t->base; return SQLITE_OK;
}
static int snDisconnect(sqlite3_vtab *p){ sqlite3_free(p); return SQLITE_OK; }
static int snBestIndex(sqlite3_vtab *v,sqlite3_index_info *p){
  int column,i,slot=1;
  (void)v;
  p->idxNum=0;
  for(column=2;column<=6;column++){
    int found=-1;
    for(i=0;i<p->nConstraint;i++){
      if( p->aConstraint[i].iColumn==column
          && p->aConstraint[i].op==SQLITE_INDEX_CONSTRAINT_EQ
          && p->aConstraint[i].usable ) { found=i; break; }
    }
    if( found<0 ){
      if( column<=4 ) return SQLITE_CONSTRAINT;
      continue;
    }
    p->idxNum|=1<<column;
    p->aConstraintUsage[found].argvIndex=slot++;
    p->aConstraintUsage[found].omit=1;
  }
  p->estimatedCost=1000.0; p->estimatedRows=100;
  if( p->nOrderBy==1 && p->aOrderBy[0].iColumn==1 && !p->aOrderBy[0].desc ){
    p->orderByConsumed=1;
  }
  return SQLITE_OK;
}
static int snOpen(sqlite3_vtab *t,sqlite3_vtab_cursor **out){
  SnCursor *c=(SnCursor*)sqlite3_malloc64(sizeof(*c));
  if( !c ) return SQLITE_NOMEM;
  memset(c,0,sizeof(*c)); c->base.pVtab=t; *out=&c->base; return SQLITE_OK;
}
static int snClose(sqlite3_vtab_cursor *p){
  SnCursor *c=(SnCursor*)p;
  int i;
  for(i=0;i<5;i++) sqlite3_value_free(c->parameters[i]);
  sqlite3_free(c->hits); sqlite3_free(c); return SQLITE_OK;
}
static int snIdList(sqlite3_value *v,const SnByte **p,int *n){
  int i,bytes;
  *p=0; *n=0;
  if( sqlite3_value_type(v)!=SQLITE_BLOB ) return 0;
  bytes=sqlite3_value_bytes(v);
  if( bytes%8 || bytes>SN_MAX_BYTES ) return 0;
  *p=(const SnByte*)sqlite3_value_blob(v); *n=bytes/8;
  if( bytes && !*p ) return 0;
  for(i=1;i<*n;i++) if( snGetId(*p+8*i)<=snGetId(*p+8*(i-1)) ) return 0;
  return 1;
}
static int snIdContains(const SnByte *p,int n,sqlite3_int64 id){
  int lo=0,hi=n;
  while( lo<hi ){
    int mid=lo+(hi-lo)/2;
    sqlite3_int64 v=snGetId(p+8*mid);
    if( v<id ) lo=mid+1; else hi=mid;
  }
  return lo<n && snGetId(p+8*lo)==id;
}
static int snFilter(sqlite3_vtab_cursor *cur,int idx,const char *name,
                    int argc,sqlite3_value **argv){
  SnCursor *c=(SnCursor*)cur;
  SnTable *t=(SnTable*)cur->pVtab;
  const SnByte *p;
  float q[SN_MAX_DIM];
  SnU32 d,n,mode,stride;
  int bytes,k,dim,i;
  const SnByte *excluded=0,*included=0;
  int nexcluded=0,nincluded=0,arg=3,hasIncluded=0;
  SnU32 row;
  sqlite3_int64 requested;
  (void)name;
  sqlite3_free(c->hits); c->hits=0; c->count=0; c->pos=0;
  for(i=0;i<5;i++){
    sqlite3_value_free(c->parameters[i]); c->parameters[i]=0;
  }
  if( argc<3 ) return SQLITE_MISUSE;
  /* Bound argument retention before copying hidden-column values. */
  for(i=0;i<argc;i++){
    int limit=i==1 ? SN_MAX_DIM*4 : snLimit(t->db);
    if( sqlite3_value_bytes(argv[i])>limit ) goto bad;
  }
  for(i=0;i<3;i++){
    c->parameters[i]=sqlite3_value_dup(argv[i]);
    if( !c->parameters[i] ) return SQLITE_NOMEM;
  }
  if( idx&(1<<5) ){
    c->parameters[3]=sqlite3_value_dup(argv[arg]);
    if( !c->parameters[3] ) return SQLITE_NOMEM;
    if( !snIdList(argv[arg++],&excluded,&nexcluded) ) goto bad;
  }
  if( idx&(1<<6) ){
    c->parameters[4]=sqlite3_value_dup(argv[arg]);
    if( !c->parameters[4] ) return SQLITE_NOMEM;
    hasIncluded=1;
    if( !snIdList(argv[arg++],&included,&nincluded) ) goto bad;
  }
  if( argc!=arg ) return SQLITE_MISUSE;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return SQLITE_OK;
  dim=snReadVector(argv[1],q);
  requested=sqlite3_value_int64(argv[2]);
  if( !dim || sqlite3_value_type(argv[2])!=SQLITE_INTEGER || requested<1 || requested>SN_MAX_K ) goto bad;
  p=(const SnByte*)sqlite3_value_blob(argv[0]); bytes=sqlite3_value_bytes(argv[0]);
  if( !p || bytes<SN_HEADER || bytes>snLimit(t->db) || memcmp(p,"SNV1",4) ) goto bad;
  d=snGet32(p+4); n=snGet32(p+8); mode=snGet32(p+12);
  if( d!=(SnU32)dim || mode>1 ) goto bad;
  stride=8+(mode ? d+4 : 4*d);
  if( (SnU64)n*stride+SN_HEADER!=(SnU64)bytes ) goto bad;
  k=(int)requested; if( n<(SnU32)k ) k=(int)n;
  if( k==0 ) return SQLITE_OK;
  c->hits=(SnHit*)sqlite3_malloc64((SnU64)k*sizeof(SnHit));
  if( !c->hits ) return SQLITE_NOMEM;
  p+=SN_HEADER;
  for(row=0;row<n;row++,p+=stride){
    SnHit h;
    h.id=snGetId(p); h.distance=0;
    if( (row&255)==0 && sqlite3_is_interrupted(t->db) ) return SQLITE_INTERRUPT;
    if( nexcluded && snIdContains(excluded,nexcluded,h.id) ) continue;
    if( hasIncluded && !snIdContains(included,nincluded,h.id) ) continue;
    if( mode ){
      float scale=snGetFloat(p+8);
      if( !snFinite(scale) || scale<0 ) goto bad;
      h.distance=t->backend->quant(p+12,scale,q,(int)d);
    }else{
      /* The kernel validates coordinates in the same pass as scoring. */
      h.distance=t->backend->distance(p+8,q,(int)d);
    }
    if( h.distance<0 ) goto bad;
    snHeapOffer(c,h,k);
  }
  qsort(c->hits,(size_t)c->count,sizeof(SnHit),snHitCompare);
  return SQLITE_OK;
bad:
  sqlite3_free(c->hits); c->hits=0; c->count=0;
  sqlite3_free(t->base.zErrMsg);
  t->base.zErrMsg=sqlite3_mprintf("invalid vector block/query or k outside 1..65536");
  return SQLITE_ERROR;
}
static int snNext(sqlite3_vtab_cursor *p){ ((SnCursor*)p)->pos++; return SQLITE_OK; }
static int snEof(sqlite3_vtab_cursor *p){ SnCursor *c=(SnCursor*)p; return c->pos>=c->count; }
static int snColumn(sqlite3_vtab_cursor *p,sqlite3_context *ctx,int i){
  SnCursor *c=(SnCursor*)p;
  if( i==0 ) sqlite3_result_int64(ctx,c->hits[c->pos].id);
  else if( i==1 ) sqlite3_result_double(ctx,c->hits[c->pos].distance);
  else if( i>=2 && i<=6 && c->parameters[i-2] ){
    sqlite3_result_value(ctx,c->parameters[i-2]);
  }
  return SQLITE_OK;
}
static int snRowid(sqlite3_vtab_cursor *p,sqlite3_int64 *id){
  *id=((SnCursor*)p)->pos+1; return SQLITE_OK;
}
static sqlite3_module snModule={
  0,0,snConnect,snBestIndex,snDisconnect,0,snOpen,snClose,snFilter,
  snNext,snEof,snColumn,snRowid,0,0,0,0,0,0,0,0,0,0,0,0
};

#ifdef _WIN32
__declspec(dllexport)
#endif
int sqlite3_sqlnew_init(sqlite3 *db,char **err,const sqlite3_api_routines *api){
  int rc,flags=SQLITE_UTF8|SQLITE_DETERMINISTIC|SQLITE_INNOCUOUS;
  const SnBackend *backend=&snScalar;
  SQLITE_EXTENSION_INIT2(api);
  (void)err;
#if SN_X86
  if( snHasAvx2() ) backend=&snAvx;
#endif
  rc=sqlite3_create_function(db,"sqlnew_pack",1,flags,0,snPack,0,0);
  if( rc==SQLITE_OK ) rc=sqlnew_fold_init(db);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_version",0,SQLITE_UTF8,0,snVersion,0,0);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_unpack",1,flags,0,snUnpack,0,0);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_l2",2,flags,(void*)backend,snL2,0,0);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_l2_scalar",2,flags,(void*)&snScalar,snL2,0,0);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_backend",0,SQLITE_UTF8,(void*)backend,snBackendName,0,0);
  if( rc==SQLITE_OK ) rc=sqlite3_create_function(db,"sqlnew_vector_block",3,flags,0,0,snBlockStep,snBlockFinal);
  if( rc==SQLITE_OK ) rc=sqlite3_create_module(db,"sqlnew_topk",&snModule,(void*)backend);
  if( rc==SQLITE_OK ) rc=sqlnew_int_init(db);
  if( rc==SQLITE_OK ) rc=sqlnew_columnar_init(db);
  if( rc==SQLITE_OK ) rc=sqlnew_region_init(db);
  return rc;
}
#ifdef SQLITE_CORE
int sqlnew_bootstrap(const char *unused){
  (void)unused;
  return sqlite3_auto_extension((void(*)(void))sqlite3_sqlnew_init);
}
#endif
