/* Exact numeric folding over the ring of 64-bit words. */
#include "sqlite3ext.h"
#ifdef SQLNEW_FOLD_STANDALONE
SQLITE_EXTENSION_INIT1
#else
SQLITE_EXTENSION_INIT3
#endif
#include <stdint.h>
#include <string.h>
#include <limits.h>

#define SF_TILE 128
#define SF_HEAD 32
#define SF_MAX (64*1024*1024)
typedef sqlite3_uint64 SfWord;
typedef unsigned char SfByte;
#if defined(_WIN32)
# define SF_API __declspec(dllexport)
#else
# define SF_API
#endif

static SfWord sfGet(const SfByte *p){
  SfWord v=0;
  int i;
  for(i=7;i>=0;i--) v=(v<<8)|p[i];
  return v;
}
static void sfPut(SfByte *p,SfWord v){
  int i;
  for(i=0;i<8;i++){ p[i]=(SfByte)v; v>>=8; }
}
static int sfWidth(SfWord v){
  int w=0;
  while( v ){ w++; v>>=1; }
  return w;
}
/* Unsigned arithmetic is intentional, including differences across INT64_MIN. */
static SfWord sfZ(SfWord v){ return (v<<1)^((SfWord)0-(v>>63)); }
static SfWord sfUnZ(SfWord v){ return (v>>1)^((SfWord)0-(v&1)); }
static SfWord sfMask(int w){ return w==64 ? UINT64_MAX : (((SfWord)1<<w)-1); }

static void sfBitsPut(SfByte *p,int bit,int w,SfWord v){
  int shift=bit&7,bytes=(w+shift+7)/8,i;
  SfWord low=v<<shift;
  p+=bit/8;
  for(i=0;i<bytes && i<8;i++){ p[i]|=(SfByte)low; low>>=8; }
  if( bytes>8 ) p[8]|=(SfByte)(v>>(64-shift));
}
static SfWord sfBitsGet(const SfByte *p,int bit,int w){
  int shift=bit&7,bytes=(w+shift+7)/8,i;
  SfWord v=0;
  p+=bit/8;
  for(i=bytes<8 ? bytes-1 : 7;i>=0;i--) v=(v<<8)|p[i];
  v>>=shift;
  if( bytes>8 ) v|=(SfWord)p[8]<<(64-shift);
  return v&sfMask(w);
}

/* A tile is a canonical minimum-size choice, with mode order breaking ties.
** 0 raw; 1 offsets; 2 first differences; 3 second differences; 4 XOR.
** Every nonraw mode stores the minimum residual and its packed offset width.
** No floating arithmetic, approximate fitting, or content hash is involved. */
static int sfTilePack(const SfWord *x,int n,SfByte *out){
  SfWord residual[SF_TILE],bestValues[SF_TILE],bestBase=0;
  int mode,best=4+8*n,bestMode=0,bestWidth=64,i,start,bestStart=0;
  for(mode=1;mode<=4;mode++){
    SfWord lo=UINT64_MAX,hi=0;
    int width,bytes;
    start=mode==1 ? 0 : mode==3 ? 2 : 1;
    if( n<=start ) continue;
    for(i=start;i<n;i++){
      SfWord r=mode==1 ? x[i] : mode==2 ? sfZ(x[i]-x[i-1])
          : mode==3 ? sfZ(x[i]-2*x[i-1]+x[i-2]) : x[i]^x[i-1];
      residual[i-start]=r;
      if( r<lo ) lo=r;
      if( r>hi ) hi=r;
    }
    width=sfWidth(hi-lo);
    bytes=12+8*start+((n-start)*width+7)/8;
    if( bytes<best ){
      best=bytes; bestMode=mode; bestWidth=width; bestBase=lo; bestStart=start;
      memcpy(bestValues,residual,(size_t)(n-start)*8);
    }
  }
  memset(out,0,(size_t)best);
  out[0]=(SfByte)bestMode; out[1]=(SfByte)bestWidth;
  if( !bestMode ){
    for(i=0;i<n;i++) sfPut(out+4+8*i,x[i]);
  }else{
    sfPut(out+4,bestBase);
    for(i=0;i<bestStart;i++) sfPut(out+12+8*i,x[i]);
    for(i=0;i<n-bestStart;i++){
      sfBitsPut(out+12+8*bestStart,i*bestWidth,bestWidth,bestValues[i]-bestBase);
    }
  }
  return best;
}
int sqlnew_fold_probe(const void *raw,int bytes){
  const SfByte *p=(const SfByte*)raw;
  SfWord words[SF_TILE];
  SfByte encoded[4+8*SF_TILE];
  int i,n=bytes/8;
  if( bytes<0 || bytes%8 ) return -1;
  if( n>SF_TILE ) n=SF_TILE;
  for(i=0;i<n;i++) words[i]=sfGet(p+8*i);
  return sfTilePack(words,n,encoded);
}
static int sfTileSize(const SfByte *p,int bytes,int n){
  int mode,width,start,size;
  if( bytes<4 ) return -1;
  mode=p[0]; width=p[1];
  if( mode>4 || width>64 || p[2] || p[3] ) return -1;
  if( mode==0 ) size=width==64 ? 4+8*n : -1;
  else{
    start=mode==1 ? 0 : mode==3 ? 2 : 1;
    if( n<=start ) return -1;
    size=12+8*start+((n-start)*width+7)/8;
  }
  return size<=bytes ? size : -1;
}
static int sfTileRead(const SfByte *p,int bytes,int n,SfWord *x){
  int size=sfTileSize(p,bytes,n),i,mode,width,start;
  SfWord base;
  if( size<0 ) return -1;
  mode=p[0]; width=p[1];
  if( mode==0 ){
    for(i=0;i<n;i++) x[i]=sfGet(p+4+8*i);
    return size;
  }
  base=sfGet(p+4); start=mode==1 ? 0 : mode==3 ? 2 : 1;
  for(i=0;i<start;i++) x[i]=sfGet(p+12+8*i);
  for(i=start;i<n;i++){
    SfWord r=base+sfBitsGet(p+12+8*start,(i-start)*width,width);
    x[i]=mode==1 ? r : mode==2 ? x[i-1]+sfUnZ(r)
        : mode==3 ? 2*x[i-1]-x[i-2]+sfUnZ(r) : x[i-1]^r;
  }
  return size;
}
static int sfChunk(SfWord origin,SfWord remaining){
  int n=SF_TILE-(int)(origin&(SF_TILE-1));
  return remaining<(SfWord)n ? (int)remaining : n;
}
static void sfHeader(SfByte *p,SfWord origin,SfWord count){
  memset(p,0,SF_HEAD); memcpy(p,"SFL1",4); p[4]=7;
  sfPut(p+8,origin); sfPut(p+16,count);
}
static int sfReadHeader(const SfByte *p,int bytes,SfWord *origin,SfWord *count){
  if( bytes<SF_HEAD || memcmp(p,"SFL1",4) || p[4]!=7 || p[5] || p[6] || p[7] || sfGet(p+24) ) return 0;
  *origin=sfGet(p+8); *count=sfGet(p+16);
  return *count<=(SF_MAX/8) && *origin<=UINT64_MAX-*count;
}

SF_API int sqlnew_fold_pack(const void *raw,int bytes,SfWord origin,void **output,int *size){
  SfWord words[SF_TILE],count;
  const SfByte *p=(const SfByte*)raw;
  SfByte *out;
  int n,i,w=SF_HEAD;
  *output=0; *size=0;
  if( bytes<0 || bytes>SF_MAX || bytes%8 ) return SQLITE_MISMATCH;
  count=(SfWord)(bytes/8);
  if( origin>UINT64_MAX-count ) return SQLITE_TOOBIG;
  out=(SfByte*)sqlite3_malloc64(SF_HEAD+(SfWord)bytes+4*(count/SF_TILE+2));
  if( !out ) return SQLITE_NOMEM;
  sfHeader(out,origin,count);
  while( count ){
    n=sfChunk(origin,count);
    for(i=0;i<n;i++) words[i]=sfGet(p+8*i);
    w+=sfTilePack(words,n,out+w); p+=8*n; origin+=n; count-=n;
  }
  *output=out; *size=w; return SQLITE_OK;
}
SF_API int sqlnew_fold_unpack(const void *frame,int bytes,void **output,int *size){
  const SfByte *p=(const SfByte*)frame;
  SfWord origin,count,words[SF_TILE];
  SfByte *out;
  int n,i,r=SF_HEAD,w=0,tile;
  *output=0; *size=0;
  if( !sfReadHeader(p,bytes,&origin,&count) ) return SQLITE_MISMATCH;
  out=(SfByte*)sqlite3_malloc64(count*8+1);
  if( !out ) return SQLITE_NOMEM;
  while( count ){
    n=sfChunk(origin,count); tile=sfTileRead(p+r,bytes-r,n,words);
    if( tile<0 ){ sqlite3_free(out); return SQLITE_CORRUPT; }
    for(i=0;i<n;i++) sfPut(out+w+8*i,words[i]);
    r+=tile; w+=8*n; origin+=n; count-=n;
  }
  if( r!=bytes ){ sqlite3_free(out); return SQLITE_CORRUPT; }
  *output=out; *size=w; return SQLITE_OK;
}

/* The global 128-word grid makes the representation independent of splits.
** Only the two pieces of the boundary tile require reconstruction. */
SF_API int sqlnew_fold_merge(const void *left,int nl,const void *right,int nr,void **output,int *size){
  const SfByte *a=(const SfByte*)left,*b=(const SfByte*)right;
  SfWord ao,an,bo,bn,pos,leftCount,words[SF_TILE];
  SfByte *out;
  int r=SF_HEAD,last=SF_HEAD,lastN=0,n,tile,w,firstN,firstSize;
  *output=0; *size=0;
  if( !sfReadHeader(a,nl,&ao,&an) || !sfReadHeader(b,nr,&bo,&bn)
      || ao+an!=bo || an+bn>SF_MAX/8 ) return SQLITE_MISMATCH;
  out=(SfByte*)sqlite3_malloc64((SfWord)nl+nr+8*SF_TILE+32);
  if( !out ) return SQLITE_NOMEM;
  sfHeader(out,ao,an+bn);
  if( !an || !bn || !(bo&(SF_TILE-1)) ){
    memcpy(out+SF_HEAD,a+SF_HEAD,(size_t)(nl-SF_HEAD));
    memcpy(out+nl,b+SF_HEAD,(size_t)(nr-SF_HEAD));
    *output=out; *size=nl+nr-SF_HEAD; return SQLITE_OK;
  }
  pos=ao; leftCount=an;
  while( leftCount ){
    n=sfChunk(pos,leftCount); last=r; lastN=n;
    tile=sfTileSize(a+r,nl-r,n);
    if( tile<0 ){ sqlite3_free(out); return SQLITE_CORRUPT; }
    r+=tile; pos+=n; leftCount-=n;
  }
  if( r!=nl ){ sqlite3_free(out); return SQLITE_CORRUPT; }
  firstN=sfChunk(bo,bn);
  tile=sfTileRead(a+last,nl-last,lastN,words);
  firstSize=sfTileRead(b+SF_HEAD,nr-SF_HEAD,firstN,words+lastN);
  if( tile<0 || firstSize<0 || lastN+firstN>SF_TILE ){
    sqlite3_free(out); return SQLITE_CORRUPT;
  }
  memcpy(out+SF_HEAD,a+SF_HEAD,(size_t)(last-SF_HEAD)); w=last;
  w+=sfTilePack(words,lastN+firstN,out+w);
  memcpy(out+w,b+SF_HEAD+firstSize,(size_t)(nr-SF_HEAD-firstSize));
  w+=nr-SF_HEAD-firstSize;
  *output=out; *size=w; return SQLITE_OK;
}
static void sfPackSql(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  void *out=0; int size=0,rc;
  SfWord origin=argc>1 ? (SfWord)sqlite3_value_int64(argv[1]) : 0;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return;
  rc=sqlnew_fold_pack(sqlite3_value_blob(argv[0]),sqlite3_value_bytes(argv[0]),origin,&out,&size);
  if( rc ) sqlite3_result_error_code(ctx,rc);
  else sqlite3_result_blob(ctx,out,size,sqlite3_free);
}
static void sfUnpackSql(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  void *out=0; int size=0,rc;
  (void)argc;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL ) return;
  rc=sqlnew_fold_unpack(sqlite3_value_blob(argv[0]),sqlite3_value_bytes(argv[0]),&out,&size);
  if( rc ) sqlite3_result_error_code(ctx,rc);
  else sqlite3_result_blob(ctx,out,size,sqlite3_free);
}
static void sfMergeSql(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  void *out=0; int size=0,rc;
  (void)argc;
  if( sqlite3_value_type(argv[0])==SQLITE_NULL || sqlite3_value_type(argv[1])==SQLITE_NULL ) return;
  rc=sqlnew_fold_merge(sqlite3_value_blob(argv[0]),sqlite3_value_bytes(argv[0]),
      sqlite3_value_blob(argv[1]),sqlite3_value_bytes(argv[1]),&out,&size);
  if( rc ) sqlite3_result_error_code(ctx,rc);
  else sqlite3_result_blob(ctx,out,size,sqlite3_free);
}
int sqlnew_fold_init(sqlite3 *db){
  int rc;
  rc=sqlite3_create_function(db,"sqlnew_fold",1,SQLITE_UTF8|SQLITE_DETERMINISTIC,0,sfPackSql,0,0);
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_fold",2,SQLITE_UTF8|SQLITE_DETERMINISTIC,0,sfPackSql,0,0);
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_unfold",1,SQLITE_UTF8|SQLITE_DETERMINISTIC,0,sfUnpackSql,0,0);
  if( !rc ) rc=sqlite3_create_function(db,"sqlnew_fold_merge",2,SQLITE_UTF8|SQLITE_DETERMINISTIC,0,sfMergeSql,0,0);
  return rc;
}
#ifdef SQLNEW_FOLD_STANDALONE
SF_API int sqlite3_fold_init(sqlite3 *db,char **error,const sqlite3_api_routines *api){
  (void)error; SQLITE_EXTENSION_INIT2(api);
  return sqlnew_fold_init(db);
}
#endif
