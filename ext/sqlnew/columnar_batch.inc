/* Bounded native grouping. The compiler retains a general SQL fallback. */
#define CS_BATCH_BYTES (8*1024*1024)
typedef struct CsBatchAcc CsBatchAcc;
struct CsBatchAcc { CsInt count; CsWide integer; double sum,error; int overflow; CsCell best; };
typedef struct CsGroup CsGroup;
struct CsGroup { CsGroup *chain,*next; CsUnsigned hash; };
typedef struct CsBatch CsBatch;
struct CsBatch {
  int ng,na,nw,keys[CS_COLS],desc[CS_COLS],ops[CS_COLS],cols[CS_COLS],types[CS_COLS];
  int cap,n,started;
  CsUnsigned bytes;
  sqlite3_stmt *direct;
  CsGroup **slots,*first,*last,*current;
};
static CsCell *csGroupKeys(CsGroup *g){ return (CsCell*)(g+1); }
static CsBatchAcc *csGroupAcc(CsBatch *b,CsGroup *g){ return (CsBatchAcc*)(csGroupKeys(g)+b->ng); }
static void csBatchFree(void *ptr){
  CsBatch *b=(CsBatch*)ptr;
  CsGroup *g,*next;
  int i;
  if( !b ) return;
  for(g=b->first;g;g=next){
    CsCell *keys=csGroupKeys(g);
    CsBatchAcc *acc=csGroupAcc(b,g);
    next=g->next;
    for(i=0;i<b->ng;i++) csCellClear(&keys[i]);
    for(i=0;i<b->na;i++) csCellClear(&acc[i].best);
    sqlite3_free(g);
  }
  sqlite3_finalize(b->direct); sqlite3_free(b->slots); sqlite3_free(b);
}
static void csBatchAdd(volatile CsBatchAcc *a,volatile double value){
  volatile double sum=a->sum,total=sum+value;
  if( fabs(sum)>fabs(value) ) a->error+=(sum-total)+value;
  else a->error+=(value-total)+sum;
  a->sum=total;
}
static int csBatchStep(CsBatch *b,CsBatchAcc *a,int op,const CsCell *v){
  if( op!=0 && !v->type ) return SQLITE_OK;
  if( a->count==INT64_MAX ) return SQLITE_TOOBIG;
  a->count++;
  if( op==2 || op==5 ){
    if( v->type==SQLITE_INTEGER ){
      CsInt low=v->v.i%16384;
      a->integer=csAdd(a->integer,csWide(v->v.i));
      if( !csFits(a->integer) ) a->overflow=1;
      if( op==5 ){
        csBatchAdd(a,(double)(v->v.i-low)); csBatchAdd(a,(double)low);
      }
    }else csBatchAdd(a,v->v.r);
  }else if( op==3 || op==4 ){
    if( !a->best.type || (op==3 ? csCellCmp(v,&a->best)<0 : csCellCmp(v,&a->best)>0) ){
      CsUnsigned before=(a->best.type==SQLITE_TEXT || a->best.type==SQLITE_BLOB) ? (CsUnsigned)a->best.n+1 : 0;
      CsUnsigned after=(v->type==SQLITE_TEXT || v->type==SQLITE_BLOB) ? (CsUnsigned)v->n+1 : 0;
      if( after>CS_BATCH_BYTES || b->bytes-before>CS_BATCH_BYTES-after ) return SQLITE_TOOBIG;
      b->bytes=b->bytes-before+after;
      csCellClear(&a->best); return csCellCopy(&a->best,v);
    }
  }
  return SQLITE_OK;
}
static int csBatchGroup(CsBatch *b,CsCell **keys,CsGroup **out){
  CsUnsigned hash=1469598103934665603ULL,size;
  CsGroup *g;
  int i,j,slot;
  for(i=0;i<b->ng;i++){
    CsCell *v=keys[i];
    const CsByte *p;
    int n;
    CsUnsigned bits=0;
    hash=(hash^(CsUnsigned)v->type)*1099511628211ULL;
    if( v->type==SQLITE_INTEGER ) bits=(CsUnsigned)v->v.i;
    else if( v->type==SQLITE_FLOAT ){ double r=v->v.r==0.0 ? 0.0 : v->v.r; memcpy(&bits,&r,8); }
    if( v->type==SQLITE_TEXT || v->type==SQLITE_BLOB ){ p=v->v.p; n=v->n; }
    else { p=(const CsByte*)&bits; n=v->type ? 8 : 0; }
    hash=(hash^(CsUnsigned)n)*1099511628211ULL;
    for(j=0;j<n;j++) hash=(hash^p[j])*1099511628211ULL;
  }
  slot=(int)(hash&(CsUnsigned)(b->cap-1));
  for(g=b->slots[slot];g;g=g->chain){
    if( g->hash!=hash ) continue;
    for(i=0;i<b->ng;i++) if( !csCellEqual(keys[i],&csGroupKeys(g)[i]) ) break;
    if( i==b->ng ){ *out=g; return SQLITE_OK; }
  }
  size=sizeof(CsGroup)+(CsUnsigned)b->ng*sizeof(CsCell)+(CsUnsigned)b->na*sizeof(CsBatchAcc);
  for(i=0;i<b->ng;i++) if( keys[i]->type==SQLITE_TEXT || keys[i]->type==SQLITE_BLOB ) size+=(CsUnsigned)keys[i]->n+1;
  if( size>CS_BATCH_BYTES || b->bytes>CS_BATCH_BYTES-size ) return SQLITE_TOOBIG;
  if( b->n>=b->cap*2 ) return SQLITE_TOOBIG;
  g=(CsGroup*)sqlite3_malloc64(sizeof(CsGroup)+(CsUnsigned)b->ng*sizeof(CsCell)+(CsUnsigned)b->na*sizeof(CsBatchAcc));
  if( !g ) return SQLITE_NOMEM;
  memset(g,0,sizeof(CsGroup)+(size_t)b->ng*sizeof(CsCell)+(size_t)b->na*sizeof(CsBatchAcc));
  g->hash=hash; g->chain=b->slots[slot]; b->slots[slot]=g;
  if( b->last ) b->last->next=g; else b->first=g;
  b->last=g; b->n++; b->bytes+=size;
  for(i=0;i<b->ng;i++){
    int rc=csCellCopy(&csGroupKeys(g)[i],keys[i]);
    if( rc ) return rc;
  }
  *out=g; return SQLITE_OK;
}
static int csGroupCompare(CsBatch *b,CsGroup *a,CsGroup *c){
  int i;
  for(i=0;i<b->ng;i++){
    CsCell *x=&csGroupKeys(a)[i],*y=&csGroupKeys(c)[i];
    int cmp=!x->type ? (y->type ? -1 : 0) : !y->type ? 1 : csCellCmp(x,y);
    if( cmp ) return b->desc[i] ? (cmp>0 ? -1 : 1) : (cmp>0 ? 1 : -1);
  }
  return 0;
}
static int csBatchConsume(CsBatch *b,CsCell **columns){
  CsCell *keys[CS_COLS],empty;
  CsGroup *g=0;
  CsBatchAcc *acc;
  int i,rc;
  memset(&empty,0,sizeof(empty));
  for(i=0;i<b->ng;i++) keys[i]=columns[b->keys[i]];
  rc=csBatchGroup(b,keys,&g);
  if( rc ) return rc;
  acc=csGroupAcc(b,g);
  for(i=0;!rc && i<b->na;i++) rc=csBatchStep(b,&acc[i],b->ops[i],b->ops[i] ? columns[b->cols[i]] : &empty);
  return rc;
}
static void csBatchSort(CsBatch *b){
  int width;
  for(width=1;width<b->n;width*=2){
    CsGroup *left=b->first,*first=0,*last=0;
    while( left ){
      CsGroup *right=left,*next,*take;
      int i,nl=0,nr=0;
      for(i=0;i<width && right;i++){ right=right->next; nl++; }
      next=right;
      for(i=0;i<width && next;i++){ next=next->next; nr++; }
      while( nl || nr ){
        if( nl && (!nr || csGroupCompare(b,left,right)<=0) ){ take=left; left=left->next; nl--; }
        else { take=right; right=right->next; nr--; }
        if( last ) last->next=take; else first=take;
        last=take;
      }
      left=next;
    }
    if( last ) last->next=0;
    b->first=first; b->last=last;
  }
}
static int csPlanInt(const char **text,int *value){
  char *end;
  long v=strtol(*text,&end,10);
  if( end==*text || *end!=',' || v<-1 || v>255 ) return SQLITE_ERROR;
  *text=end+1; *value=(int)v; return SQLITE_OK;
}
void sqlnew_columnar_batch(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsRegistry *r=(CsRegistry*)sqlite3_user_data(ctx);
  CsTable *t=0;
  CsBatch *b=0;
  CsCursor *c=0;
  sqlite3_vtab_cursor *cursor=0;
  const char *plan;
  sqlite3_str *filter=0;
  char *where=0;
  CsUnsigned mask=0;
  int i,j,rc=SQLITE_OK,fallback=0,nexact=0,nused=0,used[CS_COLS];
  if( argc<3 ) goto bad;
  t=csFind(r,(const char*)sqlite3_value_text(argv[0]),(const char*)sqlite3_value_text(argv[1]));
  plan=(const char*)sqlite3_value_text(argv[2]);
  if( !t || !plan ) goto bad;
  b=(CsBatch*)sqlite3_malloc64(sizeof(*b));
  if( !b ){ rc=SQLITE_NOMEM; goto done; }
  memset(b,0,sizeof(*b)); b->cap=8192;
  b->bytes=sizeof(*b)+(CsUnsigned)b->cap*sizeof(CsGroup*);
  b->slots=(CsGroup**)sqlite3_malloc64((CsUnsigned)b->cap*sizeof(CsGroup*));
  if( !b->slots ){ rc=SQLITE_NOMEM; goto done; }
  memset(b->slots,0,(size_t)b->cap*sizeof(CsGroup*));
  if( csPlanInt(&plan,&b->ng) || csPlanInt(&plan,&b->na) || csPlanInt(&plan,&b->nw)
      || b->ng<0 || b->ng>CS_COLS || b->na<0 || b->na>CS_COLS || b->nw<0 || argc!=b->nw+3 ) goto bad;
  for(i=0;i<b->ng;i++){
    if( csPlanInt(&plan,&b->keys[i]) || csPlanInt(&plan,&b->desc[i]) || b->keys[i]<0 || b->keys[i]>=t->ncol
        || b->desc[i]<0 || b->desc[i]>1 ) goto bad;
    mask|=(CsUnsigned)1<<b->keys[i];
  }
  for(i=0;i<b->na;i++){
    if( csPlanInt(&plan,&b->ops[i]) || csPlanInt(&plan,&b->cols[i]) || b->ops[i]<0 || b->ops[i]>5
        || (b->ops[i]!=0 && (b->cols[i]<0 || b->cols[i]>=t->ncol))
        || ((b->ops[i]==2 || b->ops[i]==5) && t->types[b->cols[i]]!=SQLITE_INTEGER && t->types[b->cols[i]]!=SQLITE_FLOAT) ) goto bad;
    if( b->ops[i]!=0 ) mask|=(CsUnsigned)1<<b->cols[i];
    b->types[i]=b->ops[i] ? t->types[b->cols[i]] : SQLITE_INTEGER;
  }
  filter=sqlite3_str_new(t->db);
  for(i=0;i<b->nw;i++){
    int col,op,kind;
    if( csPlanInt(&plan,&col) || csPlanInt(&plan,&op) || col<-1 || col>=t->ncol
      || (op!=SQLITE_INDEX_CONSTRAINT_EQ && op!=SQLITE_INDEX_CONSTRAINT_GT && op!=SQLITE_INDEX_CONSTRAINT_GE
          && op!=SQLITE_INDEX_CONSTRAINT_LT && op!=SQLITE_INDEX_CONSTRAINT_LE) ) goto bad;
    kind=sqlite3_value_type(argv[i+3]);
    if( col<0 || t->types[col]==SQLITE_INTEGER || t->types[col]==SQLITE_FLOAT ) kind=sqlite3_value_numeric_type(argv[i+3]);
    if( kind!=SQLITE_NULL && kind!=(col<0 ? SQLITE_INTEGER : t->types[col]) ) fallback=1;
    if( col>=0 ) mask|=(CsUnsigned)1<<col;
    sqlite3_str_appendf(filter,"%d,%d;",col,op); nexact++;
  }
  if( *plan ) goto bad;
  if( !t->binaryUtf8 ){
    for(i=0;i<t->ncol;i++) if( (mask&((CsUnsigned)1<<i)) && t->types[i]==SQLITE_TEXT ) fallback=1;
  }
  rc=sqlite3_str_errcode(filter);
  where=sqlite3_str_finish(filter); filter=0;
  if( rc ) goto done;
  if( !where ) where=sqlite3_mprintf("");
  if( !where ){ rc=SQLITE_NOMEM; goto done; }
  if( fallback ) goto done;
  for(i=0;i<t->ncol;i++) if( mask&((CsUnsigned)1<<i) ) used[nused++]=i;
  rc=csOpen(&t->base,&cursor); c=(CsCursor*)cursor;
  if( !rc ){
    int index=nexact ? csIndexForFilter(t,mask,where) : 0;
    if( index>0 && !b->ng && b->na ){
      /* A covered scalar aggregate can run directly in the B-tree VM. This
      ** avoids a virtual-column callback and value conversion for every row. */
      rc=csIndexPrepare(c,index,where,nexact,argv+3,b->na,b->ops,b->cols);
      if( !rc ) rc=sqlite3_step(c->indexScan);
      if( rc==SQLITE_ROW ){
        CsInt rows=sqlite3_column_int64(c->indexScan,b->na);
        t->indexReads+=rows; t->batchRows+=rows;
        b->direct=c->indexScan; c->indexScan=0; rc=SQLITE_OK;
      }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
      if( rc==SQLITE_ERROR ) sqlite3_result_error(ctx,sqlite3_errmsg(t->db),-1);
      goto result;
    }
    rc=index<0 ? -index : csFilter(cursor,index,where,nexact,argv+3);
  }
  if( !rc && !c->eof ) t->rowsOut--;
  if( !rc && !b->ng ){
    CsGroup *g;
    rc=csBatchGroup(b,0,&g);
  }
  while( !rc && !c->eof ){
    CsBlock *block=c->block;
    if( c->indexScan ){
      CsCell cells[CS_COLS],*columns[CS_COLS];
      for(i=0;i<nused;i++){
        memset(&cells[used[i]],0,sizeof(CsCell)); columns[used[i]]=&cells[used[i]];
      }
      for(i=0;!rc && i<c->indexCount;i++){
        int col=c->indexCols[i];
        if( mask&((CsUnsigned)1<<col) ){
          rc=csFromValue(t,&cells[col],sqlite3_column_value(c->indexScan,i+1),t->types[col]);
          columns[col]=&cells[col];
        }
      }
      if( !rc ) rc=csBatchConsume(b,columns);
      for(i=0;i<nused;i++) csCellClear(&cells[used[i]]);
      if( !rc ){ t->batchRows++; rc=csNext(cursor); if( !rc && !c->eof ) t->rowsOut--; }
      continue;
    }
    for(i=0;i<CS_MAP && !c->selected[i];i++){}
    if( i==CS_MAP ){
      rc=sqlite3_is_interrupted(t->db) ? SQLITE_INTERRUPT : csNextBlock(c);
      continue;
    }
    for(i=0;!rc && i<nused;i++) rc=csLoadColumn(t,block,used[i],c->column);
    for(j=0;!rc && j<CS_ROWS;j++) if( csBit(c->selected,j) ){
      CsCell *columns[CS_COLS];
      CsInt id=csRowid(block->id,j);
      if( c->hasLower && (id<c->lower || (c->strictLower && id==c->lower)) ) continue;
      if( c->hasUpper && (id>c->upper || (c->strictUpper && id==c->upper)) ) continue;
      for(i=0;i<nused;i++) columns[used[i]]=&block->cells[used[i]][j];
      rc=csBatchConsume(b,columns);
      t->batchRows++;
    }
    if( !rc && sqlite3_is_interrupted(t->db) ) rc=SQLITE_INTERRUPT;
    if( !rc ) rc=csNextBlock(c);
  }
  if( rc==SQLITE_TOOBIG ){ fallback=1; rc=SQLITE_OK; }
result:
  if( !rc && !fallback ){
    csBatchSort(b);
    sqlite3_result_pointer(ctx,b,"sqlnew_batch",csBatchFree); b=0;
  }
  goto done;
bad:
  rc=SQLITE_ERROR;
done:
  if( t && fallback ) t->batchFallbacks++;
  if( filter ) sqlite3_free(sqlite3_str_finish(filter));
  sqlite3_free(where);
  if( cursor ) csClose(cursor);
  csBatchFree(b);
  if( rc ) sqlite3_result_error_code(ctx,rc);
}
void sqlnew_columnar_batch_next(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsBatch *b=(CsBatch*)sqlite3_value_pointer(argv[0],"sqlnew_batch");
  (void)argc;
  if( !b ){ sqlite3_result_int(ctx,-1); return; }
  if( b->direct ){
    sqlite3_result_int(ctx,b->started==0); b->started=b->started ? 2 : 1; return;
  }
  b->current=b->started ? (b->current ? b->current->next : 0) : b->first;
  b->started=1; sqlite3_result_int(ctx,b->current!=0);
}
void sqlnew_columnar_batch_value(sqlite3_context *ctx,int argc,sqlite3_value **argv){
  CsBatch *b=(CsBatch*)sqlite3_value_pointer(argv[0],"sqlnew_batch");
  int col=sqlite3_value_int(argv[1]);
  (void)argc;
  if( !b || col<0 || col>=b->ng+b->na || (b->direct ? b->started!=1 : !b->current) ){
    sqlite3_result_error_code(ctx,SQLITE_MISUSE); return;
  }
  if( b->direct ){ sqlite3_result_value(ctx,sqlite3_column_value(b->direct,col)); return; }
  if( col<b->ng ) csResult(ctx,&csGroupKeys(b->current)[col]);
  else{
    CsBatchAcc *a=&csGroupAcc(b,b->current)[col-b->ng];
    int op=b->ops[col-b->ng];
    if( op==0 || op==1 ) sqlite3_result_int64(ctx,a->count);
    else if( !a->count ) return;
    else if( op==3 || op==4 ) csResult(ctx,&a->best);
    else if( op==5 ) sqlite3_result_double(ctx,(a->sum+(isfinite(a->error) ? a->error : 0.0))/(double)a->count);
    else if( a->overflow ) sqlite3_result_error(ctx,"integer overflow",-1);
    else if( b->types[col-b->ng]==SQLITE_FLOAT ) sqlite3_result_double(ctx,a->sum+(isfinite(a->error) ? a->error : 0.0));
    else sqlite3_result_int64(ctx,csSigned(a->integer.lo));
  }
}
