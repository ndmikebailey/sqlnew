/* Covering indexes live in ordinary transactional B-trees in the same file.
** Encoded key fields make NULL sortable without a nullable primary key. */
static void csIndexesFree(CsIndex *p){
  while( p ){
    CsIndex *next=p->next;
    sqlite3_finalize(p->put); sqlite3_finalize(p->remove); sqlite3_finalize(p->check);
    sqlite3_free(p->name); sqlite3_free(p); p=next;
  }
}
static int csIndexNumber(const char **text,int *value){
  char *end;
  long n=strtol(*text,&end,10);
  if( end==*text || *end!=',' || n<0 || n>CS_COLS ) return SQLITE_CORRUPT_VTAB;
  *text=end+1; *value=(int)n; return SQLITE_OK;
}
static int csIndexesLoadMode(CsTable *t,CsIndex **out,int all){
  sqlite3_stmt *s=0;
  int rc;
  *out=0;
  rc=csPrepare(t,&s,sqlite3_mprintf("SELECT id,coalesce(name,''),definition,active FROM \"%w\".\"%w_indexes\" WHERE active OR %d ORDER BY id DESC",t->schema,t->name,all));
  while( !rc && (rc=sqlite3_step(s))==SQLITE_ROW ){
    const char *def=(const char*)sqlite3_column_text(s,2);
    const char *name=(const char*)sqlite3_column_text(s,1);
    CsIndex *p=(CsIndex*)sqlite3_malloc64(sizeof(*p));
    int i;
    if( !p ){ rc=SQLITE_NOMEM; break; }
    memset(p,0,sizeof(*p)); p->next=*out; *out=p;
    p->id=sqlite3_column_int(s,0);
    p->active=sqlite3_column_int(s,3);
    if( p->id<=0 || p->id>1000000000 || !def || !name ){ rc=SQLITE_CORRUPT_VTAB; break; }
    p->name=sqlite3_mprintf("%s",name);
    if( !p->name ){ rc=SQLITE_NOMEM; break; }
    rc=csIndexNumber(&def,&p->nkey);
    if( !rc ) rc=csIndexNumber(&def,&p->ncol);
    if( !rc && (!p->nkey || p->nkey>p->ncol) ) rc=SQLITE_CORRUPT_VTAB;
    for(i=0;!rc && i<p->ncol;i++){
      rc=csIndexNumber(&def,&p->cols[i]);
      if( !rc && (p->cols[i]>=t->ncol || (p->mask&((CsUnsigned)1<<p->cols[i]))) ) rc=SQLITE_CORRUPT_VTAB;
      if( !rc ) p->mask|=(CsUnsigned)1<<p->cols[i];
    }
    for(i=0;!rc && i<p->nkey;i++){
      rc=csIndexNumber(&def,&p->desc[i]);
      if( !rc && p->desc[i]>1 ) rc=SQLITE_CORRUPT_VTAB;
    }
    if( !rc && *def ) rc=SQLITE_CORRUPT_VTAB;
  }
  sqlite3_finalize(s);
  if( rc==SQLITE_DONE ) rc=SQLITE_OK;
  if( rc ){ csIndexesFree(*out); *out=0; }
  return rc;
}
static int csIndexesLoad(CsTable *t,CsIndex **out){ return csIndexesLoadMode(t,out,0); }
static int csIndexOrderCompatible(CsTable *t,CsIndex *p){
  int i;
  if( !t->binaryUtf8 ) for(i=0;i<p->nkey;i++) if( t->types[p->cols[i]]==SQLITE_TEXT ) return 0;
  return 1;
}
static int csIndexKey(sqlite3_stmt *s,int parameter,const CsCell *v){
  CsByte bytes[9],*text;
  CsUnsigned bits;
  int i,rc;
  bytes[0]=(CsByte)(v->type!=0);
  if( !v->type ) return sqlite3_bind_blob(s,parameter,bytes,1,SQLITE_TRANSIENT);
  if( v->type==SQLITE_INTEGER || v->type==SQLITE_FLOAT ){
    if( v->type==SQLITE_INTEGER ) bits=(CsUnsigned)v->v.i^((CsUnsigned)1<<63);
    else{
      double r=v->v.r==0.0 ? 0.0 : v->v.r;
      memcpy(&bits,&r,8);
      bits=(bits>>63) ? ~bits : bits^((CsUnsigned)1<<63);
    }
    for(i=0;i<8;i++) bytes[i+1]=(CsByte)(bits>>(56-i*8));
    return sqlite3_bind_blob(s,parameter,bytes,9,SQLITE_TRANSIENT);
  }
  text=(CsByte*)sqlite3_malloc64((CsUnsigned)v->n+1);
  if( !text ) return SQLITE_NOMEM;
  text[0]=1; if( v->n ) memcpy(text+1,v->v.p,(size_t)v->n);
  rc=sqlite3_bind_blob(s,parameter,text,v->n+1,sqlite3_free);
  return rc;
}
static int csIndexWrite(CsTable *t,CsIndex *p,CsInt id,CsCell **values,int remove){
  sqlite3_stmt **cached=remove ? &p->remove : &p->put;
  sqlite3_stmt *s;
  int i,rc=SQLITE_OK;
  if( !*cached ){
    sqlite3_str *sql=sqlite3_str_new(t->db);
    if( remove ){
      sqlite3_str_appendf(sql,"DELETE FROM \"%w\".\"%w_index_%d\" WHERE ",t->schema,t->name,p->id);
      for(i=0;i<p->nkey;i++) sqlite3_str_appendf(sql,"k%d=?%d AND ",i,i+1);
      sqlite3_str_appendf(sql,"rid=?%d",p->nkey+1);
    }else{
      sqlite3_str_appendf(sql,"INSERT INTO \"%w\".\"%w_index_%d\" VALUES(",t->schema,t->name,p->id);
      for(i=0;i<p->nkey+1+p->ncol;i++) sqlite3_str_appendf(sql,"%s?%d",i ? "," : "",i+1);
      sqlite3_str_appendchar(sql,1,')');
    }
    rc=csPrepare(t,cached,sqlite3_str_finish(sql));
    if( rc ) return rc;
  }
  s=*cached;
  for(i=0;!rc && i<p->nkey;i++) rc=csIndexKey(s,i+1,values[p->cols[i]]);
  if( !rc ) rc=sqlite3_bind_int64(s,p->nkey+1,id);
  if( !remove ) for(i=0;!rc && i<p->ncol;i++) rc=csBind(s,p->nkey+2+i,values[p->cols[i]]);
  if( !rc ) rc=sqlite3_step(s);
  if( rc==SQLITE_DONE ){ rc=SQLITE_OK; t->indexWrites++; }
  sqlite3_reset(s); sqlite3_clear_bindings(s);
  return rc;
}
static int csIndexRow(CsTable *t,CsInt id,CsCell **values,int remove){
  CsIndex *p;
  int rc=SQLITE_OK;
  for(p=t->indexes;!rc && p;p=p->next) rc=csIndexWrite(t,p,id,values,remove);
  return rc;
}
static int csIndexVerify(CsTable *t,CsIndex *p,CsInt id,CsCell **values){
  int i,rc=SQLITE_OK;
  if( !p->check ){
    sqlite3_str *sql=sqlite3_str_new(t->db);
    sqlite3_str_appendf(sql,"SELECT 1 FROM \"%w\".\"%w_index_%d\" WHERE ",t->schema,t->name,p->id);
    for(i=0;i<p->nkey;i++) sqlite3_str_appendf(sql,"k%d=?%d AND ",i,i+1);
    sqlite3_str_appendf(sql,"rid=?%d",p->nkey+1);
    for(i=0;i<p->ncol;i++) sqlite3_str_appendf(sql," AND v%d IS ?%d",i,p->nkey+2+i);
    rc=csPrepare(t,&p->check,sqlite3_str_finish(sql));
    if( rc ) return rc;
  }
  for(i=0;!rc && i<p->nkey;i++) rc=csIndexKey(p->check,i+1,values[p->cols[i]]);
  if( !rc ) rc=sqlite3_bind_int64(p->check,p->nkey+1,id);
  for(i=0;!rc && i<p->ncol;i++) rc=csBind(p->check,p->nkey+2+i,values[p->cols[i]]);
  if( !rc ){
    rc=sqlite3_step(p->check);
    if( rc==SQLITE_ROW ) rc=SQLITE_OK;
    else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  }
  sqlite3_reset(p->check); sqlite3_clear_bindings(p->check); return rc;
}
static int csIndexChange(CsTable *t,CsInt id,CsCell *incoming,CsUnsigned changed){
  CsIndex *p;
  CsBlock *b=0;
  CsCell *before[CS_COLS],*after[CS_COLS];
  CsUnsigned mask=0;
  int i,rc;
  for(p=t->indexes;p;p=p->next) if( p->mask&changed ) mask|=p->mask;
  if( !mask ) return SQLITE_OK;
  rc=csFlush(t);
  csFreeBlock(t,t->write); t->write=0;
  if( !rc ) rc=csReadBlock(t,csBlockId(id),mask,&b);
  if( rc ) return rc;
  for(i=0;i<t->ncol;i++){
    before[i]=(mask&((CsUnsigned)1<<i)) ? &b->cells[i][csSlot(id)] : 0;
    after[i]=(changed&((CsUnsigned)1<<i)) ? &incoming[i] : before[i];
  }
  for(p=t->indexes;!rc && p;p=p->next) if( p->mask&changed ){
    for(i=0;i<p->ncol;i++) if( !csCellEqual(before[p->cols[i]],after[p->cols[i]]) ) break;
    if( i==p->ncol ) continue;
    rc=csIndexWrite(t,p,id,before,1);
    if( !rc ) rc=csIndexWrite(t,p,id,after,0);
  }
  csFreeBlock(t,b); return rc;
}
static int csIndexBest(CsTable *t,sqlite3_index_info *info){
  CsIndex *list=0,*p;
  CsUnsigned used=info->colUsed&(((CsUnsigned)1<<t->ncol)-1);
  double best=info->estimatedCost;
  int i,rc=csIndexesLoad(t,&list);
  if( rc ) return rc;
  for(i=0;i<info->nConstraint;i++) if( info->aConstraint[i].usable && info->aConstraint[i].iColumn<0 ){
    csIndexesFree(list); return SQLITE_OK;
  }
  for(p=list;p;p=p->next) if( (used&~p->mask)==0 && csIndexOrderCompatible(t,p) ){
    int range=0,ordered=info->nOrderBy>0 && info->nOrderBy<=p->nkey+1,desc=0;
    double cost;
    for(i=0;i<info->nConstraint;i++){
      int op=info->aConstraint[i].op;
      if( info->aConstraint[i].usable && info->aConstraint[i].iColumn==p->cols[0]
          && (op==SQLITE_INDEX_CONSTRAINT_EQ || op==SQLITE_INDEX_CONSTRAINT_GT || op==SQLITE_INDEX_CONSTRAINT_GE
              || op==SQLITE_INDEX_CONSTRAINT_LT || op==SQLITE_INDEX_CONSTRAINT_LE)
          && info->aConstraintUsage[i].argvIndex ) range=1;
    }
    if( ordered ){
      desc=info->aOrderBy[0].desc^p->desc[0];
      for(i=0;i<info->nOrderBy;i++){
        int column=i<p->nkey ? p->cols[i] : -1;
        int direction=i<p->nkey ? p->desc[i]^desc : desc;
        if( info->aOrderBy[i].iColumn!=column || info->aOrderBy[i].desc!=direction ) ordered=0;
      }
    }
    cost=range ? 200.0+p->ncol : ordered ? 200000.0+p->ncol : 400000.0+p->ncol;
    if( cost<best ){
      best=cost; info->idxNum=p->id*2+(ordered && desc);
      info->estimatedCost=cost; info->estimatedRows=range ? 1000 : 1000000;
      info->orderByConsumed=ordered;
    }
  }
  csIndexesFree(list); return SQLITE_OK;
}
static int csIndexForFilter(CsTable *t,CsUnsigned mask,const char *plan){
  CsIndex *list=0,*p;
  int chosen=0,ncol=CS_COLS+1,rc=csIndexesLoad(t,&list);
  if( rc ) return -rc;
  for(p=list;p;p=p->next) if( !(mask&~p->mask) && p->ncol<ncol && csIndexOrderCompatible(t,p) ){
    const char *z=plan;
    while( *z ){
      char *end;
      int col=(int)strtol(z,&end,10);
      if( *end!=',' ) break;
      (void)strtol(end+1,&end,10);
      if( *end!=';' ) break;
      z=end+1;
      if( col==p->cols[0] ){ chosen=p->id*2; ncol=p->ncol; break; }
    }
  }
  csIndexesFree(list); return chosen;
}
static int csIndexPrepare(
  CsCursor *c,int id,const char *plan,int argc,sqlite3_value **argv,
  int na,const int *ops,const int *cols
){
  CsTable *t=(CsTable*)c->base.pVtab;
  CsIndex *list=0,*p;
  sqlite3_str *sql;
  CsCell *keys=0;
  int *bind=0,i,j,rc=csIndexesLoadMode(t,&list,1);
  if( rc ) return rc;
  for(p=list;p && p->id!=id/2;p=p->next){}
  if( !p ){ csIndexesFree(list); return SQLITE_SCHEMA; }
  keys=(CsCell*)sqlite3_malloc64(((CsUnsigned)argc+1)*sizeof(CsCell));
  bind=(int*)sqlite3_malloc64(((CsUnsigned)argc+1)*sizeof(int));
  if( !keys || !bind ){ sqlite3_free(keys); sqlite3_free(bind); csIndexesFree(list); return SQLITE_NOMEM; }
  memset(keys,0,((size_t)argc+1)*sizeof(CsCell)); memset(bind,0,((size_t)argc+1)*sizeof(int));
  c->indexCount=p->ncol; memcpy(c->indexCols,p->cols,(size_t)p->ncol*sizeof(int));
  sql=sqlite3_str_new(t->db);
  if( na ){
    static const char *names[]={"count","count","sum","min","max","avg"};
    sqlite3_str_appendall(sql,"SELECT ");
    for(i=0;i<na;i++){
      sqlite3_str_appendf(sql,"%s%s(",i ? "," : "",names[ops[i]]);
      if( !ops[i] ) sqlite3_str_appendchar(sql,1,'*');
      else{
        for(j=0;j<p->ncol && p->cols[j]!=cols[i];j++){}
        if( j==p->ncol ){ rc=SQLITE_MISUSE; break; }
        if( p->active ) sqlite3_str_appendf(sql,"v%d",j);
        else sqlite3_str_appendf(sql,"\"%w\"",t->names[cols[i]]);
      }
      sqlite3_str_appendchar(sql,1,')');
    }
    /* Keep visited-row accounting without exposing rows through xColumn. */
    sqlite3_str_appendall(sql,",count(*)");
  }else if( p->active ){
    sqlite3_str_appendall(sql,"SELECT rid");
    for(i=0;i<p->ncol;i++) sqlite3_str_appendf(sql,",v%d",i);
  }else{
    /* A statement prepared before index removal must retain its promised
    ** ordering. Its replacement scan forces the base store and sorts keys. */
    sqlite3_str_appendall(sql,"SELECT rowid");
    for(i=0;i<p->ncol;i++) sqlite3_str_appendf(sql,",\"%w\"",t->names[p->cols[i]]);
  }
  if( p->active ){
    sqlite3_str_appendf(sql," FROM \"%w\".\"%w_index_%d\" WHERE 1",t->schema,t->name,p->id);
  }else{
    sqlite3_str_appendf(sql," FROM \"%w\".\"%w\" WHERE rowid>=(-9223372036854775807-1)",t->schema,t->name);
  }
  for(i=0;!rc && i<argc;i++){
    char *end;
    const char *op;
    int col=(int)strtol(plan,&end,10),operation,kind;
    if( *end!=',' ){ rc=SQLITE_MISUSE; break; }
    operation=(int)strtol(end+1,&end,10);
    if( *end!=';' ){ rc=SQLITE_MISUSE; break; }
    plan=end+1;
    op=operation==SQLITE_INDEX_CONSTRAINT_EQ ? "=" : operation==SQLITE_INDEX_CONSTRAINT_GT ? ">"
      : operation==SQLITE_INDEX_CONSTRAINT_GE ? ">=" : operation==SQLITE_INDEX_CONSTRAINT_LT ? "<" : "<=";
    if( col<0 ){ sqlite3_str_appendf(sql," AND %s%s?%d",p->active ? "rid" : "rowid",op,i+1); bind[i]=2; continue; }
    for(j=0;j<p->ncol && p->cols[j]!=col;j++){}
    if( j==p->ncol ){ rc=SQLITE_MISUSE; break; }
    kind=sqlite3_value_type(argv[i]);
    if( kind==SQLITE_NULL ){ sqlite3_str_appendall(sql," AND 0"); continue; }
    if( t->types[col]==SQLITE_INTEGER || t->types[col]==SQLITE_FLOAT ) kind=sqlite3_value_numeric_type(argv[i]);
    if( !p->active ){
      sqlite3_str_appendf(sql," AND \"%w\"%s?%d",t->names[col],op,i+1); bind[i]=2;
    }else if( j<p->nkey && kind==t->types[col] ){
      rc=csFromValue(t,&keys[i],argv[i],t->types[col]);
      if( rc ) break;
      sqlite3_str_appendf(sql," AND k%d%s?%d",j,op,i+1); bind[i]=1;
      /* NULL sorts before every encoded value, but never satisfies a SQL
      ** comparison. Native aggregates also consume this cursor directly. */
      if( operation==SQLITE_INDEX_CONSTRAINT_LT || operation==SQLITE_INDEX_CONSTRAINT_LE )
        sqlite3_str_appendf(sql," AND k%d>=x'01'",j);
    }else{ sqlite3_str_appendf(sql," AND v%d%s?%d",j,op,i+1); bind[i]=2; }
  }
  if( !na ){
    sqlite3_str_appendall(sql," ORDER BY ");
    for(i=0;i<p->nkey;i++){
      if( p->active ) sqlite3_str_appendf(sql,"%sk%d%s",i ? "," : "",i,(p->desc[i]^(id&1)) ? " DESC" : "");
      else sqlite3_str_appendf(sql,"%s\"%w\"%s",i ? "," : "",t->names[p->cols[i]],(p->desc[i]^(id&1)) ? " DESC" : "");
    }
    sqlite3_str_appendf(sql,",%s%s",p->active ? "rid" : "rowid",(id&1) ? " DESC" : "");
  }
  if( rc ) sqlite3_free(sqlite3_str_finish(sql));
  else rc=csPrepare(t,&c->indexScan,sqlite3_str_finish(sql));
  for(i=0;!rc && i<argc;i++){
    if( bind[i]==1 ) rc=csIndexKey(c->indexScan,i+1,&keys[i]);
    else if( bind[i]==2 ) rc=sqlite3_bind_value(c->indexScan,i+1,argv[i]);
  }
  for(i=0;i<argc;i++) csCellClear(&keys[i]);
  sqlite3_free(keys); sqlite3_free(bind); csIndexesFree(list);
  return rc;
}
static int csIndexFilter(CsCursor *c,int id,const char *plan,int argc,sqlite3_value **argv){
  int rc=csIndexPrepare(c,id,plan,argc,argv,0,0,0);
  if( !rc ) rc=csNext(&c->base);
  return rc;
}
static int csIndexSchema(CsTable *t,const char *newName){
  CsIndex *list=0,*p;
  int rc;
  csIndexesFree(t->indexes); t->indexes=0;
  rc=csIndexesLoadMode(t,&list,1);
  for(p=list;!rc && p;p=p->next){
    if( newName ) rc=csExec(t,sqlite3_mprintf("ALTER TABLE \"%w\".\"%w_index_%d\" RENAME TO \"%w_index_%d\"",t->schema,t->name,p->id,newName,p->id));
    else rc=csExec(t,sqlite3_mprintf("DROP TABLE \"%w\".\"%w_index_%d\"",t->schema,t->name,p->id));
  }
  csIndexesFree(list); return rc;
}
static int csIndexCreate(CsTable *t,const char *command){
  sqlite3_stmt *s=0;
  sqlite3_str *sql=0,*definition=0;
  CsIndex *p=0;
  sqlite3_vtab_cursor *cursor=0;
  CsCursor *c;
  int rc=SQLITE_OK,i,j,mutated=0;
  int reuse=0;
  char *def=0;
  const char *types[]={"", "INTEGER", "REAL", "TEXT", "BLOB"};
  p=(CsIndex*)sqlite3_malloc64(sizeof(*p));
  if( !p ) return SQLITE_NOMEM;
  memset(p,0,sizeof(*p));
  rc=sqlite3_prepare_v2(t->db,"SELECT json_extract(?1,'$.create_index'),json_type(?1,'$.keys'),json_type(?1,'$.include')",-1,&s,0);
  if( !rc ){
    sqlite3_bind_text(s,1,command,-1,SQLITE_TRANSIENT); rc=sqlite3_step(s);
    if( rc==SQLITE_ROW ){
      const char *name=(const char*)sqlite3_column_text(s,0);
      const char *keys=(const char*)sqlite3_column_text(s,1),*include=(const char*)sqlite3_column_text(s,2);
      if( !name || !name[0] || !keys || strcmp(keys,"array") || (include && strcmp(include,"array")) ) rc=SQLITE_ERROR;
      else { p->name=sqlite3_mprintf("%s",name); rc=p->name ? SQLITE_OK : SQLITE_NOMEM; }
    }
  }
  sqlite3_finalize(s); s=0;
  for(j=0;!rc && j<2;j++){
    rc=sqlite3_prepare_v2(t->db,j ? "SELECT value,type,0 FROM json_each(?,'$.include')"
      : "SELECT CASE WHEN type='object' THEN json_extract(value,'$.column') ELSE value END,type,"
        "CASE WHEN type='object' THEN coalesce(json_extract(value,'$.desc'),0) ELSE 0 END FROM json_each(?,'$.keys')",-1,&s,0);
    if( !rc ) sqlite3_bind_text(s,1,command,-1,SQLITE_TRANSIENT);
    while( !rc && (rc=sqlite3_step(s))==SQLITE_ROW ){
      const char *name=(const char*)sqlite3_column_text(s,0),*type=(const char*)sqlite3_column_text(s,1);
      int direction=sqlite3_column_int(s,2);
      if( !name || !type || (strcmp(type,"text") && (j || strcmp(type,"object")))
          || direction<0 || direction>1 ){ rc=SQLITE_ERROR; break; }
      for(i=0;i<t->ncol;i++) if( !sqlite3_stricmp(name,t->names[i]) ) break;
      if( i==t->ncol || p->ncol==CS_COLS || (p->mask&((CsUnsigned)1<<i)) ){ rc=SQLITE_ERROR; break; }
      if( !j ) p->desc[p->ncol]=direction;
      p->cols[p->ncol++]=i; p->mask|=(CsUnsigned)1<<i; rc=SQLITE_OK;
    }
    if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    sqlite3_finalize(s); s=0;
    if( !j ) p->nkey=p->ncol;
  }
  if( !rc && !p->nkey ) rc=SQLITE_ERROR;
  if( !rc ){
    definition=sqlite3_str_new(t->db);
    sqlite3_str_appendf(definition,"%d,%d,",p->nkey,p->ncol);
    for(i=0;i<p->ncol;i++) sqlite3_str_appendf(definition,"%d,",p->cols[i]);
    for(i=0;i<p->nkey;i++) sqlite3_str_appendf(definition,"%d,",p->desc[i]);
    def=sqlite3_str_finish(definition); definition=0;
    if( !def ) rc=SQLITE_NOMEM;
  }
  /* An ignorable name conflict must precede every storage mutation, including
  ** flushing a pending block. The VM relies on that xUpdate contract. */
  if( !rc ) rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT 1 FROM \"%w\".\"%w_indexes\" WHERE name=?",t->schema,t->name));
  if( !rc ){
    rc=sqlite3_bind_text(s,1,p->name,-1,SQLITE_TRANSIENT);
    if( !rc ){
      rc=sqlite3_step(s);
      if( rc==SQLITE_ROW ) rc=csError(t,SQLITE_CONSTRAINT_UNIQUE,"columnar index name already exists");
      else if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    }
  }
  sqlite3_finalize(s); s=0;
  if( !rc ){ mutated=1; rc=csFlush(t); }
  if( !rc ) rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT id,1 FROM \"%w\".\"%w_indexes\" WHERE NOT active AND definition=?"
    " UNION ALL SELECT coalesce(max(id),0)+1,0 FROM \"%w\".\"%w_indexes\" ORDER BY 2 DESC LIMIT 1",t->schema,t->name,t->schema,t->name));
  if( !rc ){
    sqlite3_bind_text(s,1,def,-1,SQLITE_TRANSIENT);
    rc=sqlite3_step(s);
    if( rc==SQLITE_ROW ){ p->id=sqlite3_column_int(s,0); reuse=sqlite3_column_int(s,1); rc=p->id>0 && p->id<=1000000000 ? SQLITE_OK : SQLITE_FULL; }
  }
  sqlite3_finalize(s); s=0;
  if( rc ) goto done;
  sql=sqlite3_str_new(t->db);
  sqlite3_str_appendf(sql,"CREATE TABLE \"%w\".\"%w_index_%d\"(",t->schema,t->name,p->id);
  for(i=0;i<p->nkey;i++) sqlite3_str_appendf(sql,"k%d BLOB NOT NULL,",i);
  sqlite3_str_appendall(sql,"rid INTEGER NOT NULL");
  for(i=0;i<p->ncol;i++) sqlite3_str_appendf(sql,",v%d %s",i,types[t->types[p->cols[i]]]);
  sqlite3_str_appendall(sql,",PRIMARY KEY(");
  for(i=0;i<p->nkey;i++) sqlite3_str_appendf(sql,"k%d%s,",i,p->desc[i] ? " DESC" : "");
  sqlite3_str_appendall(sql,"rid)) WITHOUT ROWID");
  if( reuse ) sqlite3_free(sqlite3_str_finish(sql));
  else rc=csExec(t,sqlite3_str_finish(sql));
  sql=0;
  if( !rc ) rc=csOpen(&t->base,&cursor);
  c=(CsCursor*)cursor;
  if( !rc ) rc=csFilter(cursor,0,"",0,0);
  while( !rc && !c->eof ){
    CsCell *values[CS_COLS];
    for(i=0;!rc && i<p->ncol;i++) rc=csLoadColumn(t,c->block,p->cols[i],c->column);
    for(j=0;!rc && j<CS_ROWS;j++) if( csBit(c->block->live,j) ){
      for(i=0;i<p->ncol;i++) values[p->cols[i]]=&c->block->cells[p->cols[i]][j];
      rc=csIndexWrite(t,p,csRowid(c->block->id,j),values,0);
    }
    if( !rc && sqlite3_is_interrupted(t->db) ) rc=SQLITE_INTERRUPT;
    if( !rc ) rc=csNextBlock(c);
  }
  if( cursor ){ csClose(cursor); cursor=0; }
  if( rc ) goto done;
  {
    rc=csPrepare(t,&s,sqlite3_mprintf("INSERT INTO \"%w\".\"%w_indexes\" VALUES(?,?,?,1)"
      " ON CONFLICT(id) DO UPDATE SET name=excluded.name,definition=excluded.definition,active=1",t->schema,t->name));
    if( !rc ){
      sqlite3_bind_int(s,1,p->id); sqlite3_bind_text(s,2,p->name,-1,SQLITE_TRANSIENT); sqlite3_bind_text(s,3,def,-1,SQLITE_TRANSIENT);
      rc=sqlite3_step(s); if( rc==SQLITE_DONE ) rc=SQLITE_OK;
    }
  }
done:
  /* A backing-store failure after mutation must roll the statement back,
  ** even when the outer command requested IGNORE or FAIL. */
  if( mutated && (rc&255)==SQLITE_CONSTRAINT ) rc=csError(t,SQLITE_ERROR,"columnar index storage constraint");
  sqlite3_free(def);
  sqlite3_finalize(s);
  if( cursor ) csClose(cursor);
  csIndexesFree(p);
  if( !rc ){ csIndexesFree(t->indexes); t->indexes=0; rc=csIndexesLoad(t,&t->indexes); }
  return rc;
}
static int csIndexDrop(CsTable *t,const char *name){
  CsIndex *list=0,*p;
  int rc=csFlush(t),id=0;
  if( !rc ) rc=csIndexesLoad(t,&list);
  for(p=list;p;p=p->next) if( !strcmp(p->name,name) ) id=p->id;
  csIndexesFree(list);
  if( rc ) return rc;
  if( !id ) return csError(t,SQLITE_ERROR,"unknown columnar index");
  csIndexesFree(t->indexes); t->indexes=0;
  rc=csExec(t,sqlite3_mprintf("DELETE FROM \"%w\".\"%w_index_%d\";UPDATE \"%w\".\"%w_indexes\" SET active=0,name=NULL WHERE id=%d",
    t->schema,t->name,id,t->schema,t->name,id));
  if( !rc ) rc=csIndexesLoad(t,&t->indexes);
  return rc;
}
