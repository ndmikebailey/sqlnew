/* Sparse updates use the pager's transaction and savepoint boundaries. */
static int csReadBlock(CsTable *t,CsInt id,CsUnsigned mask,CsBlock **out){
  sqlite3_stmt *s=0,*column=0;
  CsBlock *b=csNewBlock(t,id);
  int rc,i;
  *out=0;
  if( !b ) return SQLITE_NOMEM;
  rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT live,n FROM \"%w\".\"%w_blocks\" WHERE block=?",t->schema,t->name));
  if( !rc ){
    sqlite3_bind_int64(s,1,id); rc=sqlite3_step(s);
    if( rc==SQLITE_ROW ){ rc=csReadLive(t,b,s,0,1); t->blocksRead++; }
    else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  }
  if( !rc ) rc=csColumnStatement(t,&column);
  for(i=0;!rc && i<t->ncol;i++) if( mask&((CsUnsigned)1<<i) ) rc=csLoadColumn(t,b,i,column);
  sqlite3_finalize(s); sqlite3_finalize(column);
  if( rc ) csFreeBlock(t,b); else *out=b;
  return rc;
}
static int csCompactColumn(CsTable *t,CsInt id,int col){
  CsBlock *b=0,*saved=t->write;
  int rc=csReadBlock(t,id,(CsUnsigned)1<<col,&b);
  if( rc ) return rc;
  b->dirty=1; b->changed=(CsUnsigned)1<<col;
  t->write=b; rc=csFlush(t); t->write=saved;
  csFreeBlock(t,b);
  if( !rc ) t->compactions++;
  return rc;
}
static int csBaseColumn(CsTable *t,CsBlock *live,int col,CsBlock **out){
  sqlite3_stmt *s=0;
  CsBlock *b=csNewBlock(t,live->id);
  int rc;
  *out=0;
  if( !b ) return SQLITE_NOMEM;
  memcpy(b->live,live->live,CS_MAP); b->count=live->count;
  rc=csAllocateColumn(b,col);
  if( !rc ) rc=csColumnStatement(t,&s);
  if( !rc ){
    sqlite3_bind_int64(s,1,b->id); sqlite3_bind_int(s,2,col); rc=sqlite3_step(s);
    if( rc==SQLITE_ROW ){
      const CsByte *p=(const CsByte*)sqlite3_column_blob(s,1);
      rc=p ? csDecode(t,b,col,p,sqlite3_column_bytes(s,1)) : SQLITE_CORRUPT_VTAB;
    }else if( rc==SQLITE_DONE ) rc=SQLITE_CORRUPT_VTAB;
  }
  sqlite3_finalize(s);
  if( rc ) csFreeBlock(t,b); else *out=b;
  return rc;
}
static int csCompact(CsTable *t){
  sqlite3_stmt *s=0;
  int rc=csFlush(t);
  csFreeBlock(t,t->write); t->write=0;
  if( !rc ) rc=csPrepare(t,&s,sqlite3_mprintf(
    "SELECT col,block FROM \"%w\".\"%w_patch\" LIMIT 1",t->schema,t->name));
  while( !rc ){
    int col;
    CsInt block;
    rc=sqlite3_step(s);
    if( rc==SQLITE_DONE ){ rc=SQLITE_OK; break; }
    if( rc!=SQLITE_ROW ) break;
    col=sqlite3_column_int(s,0); block=sqlite3_column_int64(s,1);
    sqlite3_reset(s);
    if( col<0 || col>=t->ncol ){ rc=SQLITE_CORRUPT_VTAB; break; }
    rc=csCompactColumn(t,block,col);
    if( !rc && sqlite3_is_interrupted(t->db) ) rc=SQLITE_INTERRUPT;
  }
  sqlite3_finalize(s); return rc;
}
static int csPatchUpdate(CsTable *t,CsInt id,CsCell *incoming,CsUnsigned changed){
  sqlite3_stmt *original=0,*put=0,*remove=0,*count=0;
  CsBlock *b=0;
  CsInt block=csBlockId(id);
  int slot=csSlot(id),i,rc=csFlush(t);
  csFreeBlock(t,t->write); t->write=0;
  if( !rc ) rc=csPrepare(t,&original,sqlite3_mprintf(
    "SELECT original FROM \"%w\".\"%w_patch\" WHERE col=? AND block=? AND slot=?",t->schema,t->name));
  if( !rc ) rc=csPrepare(t,&put,sqlite3_mprintf(
    "INSERT INTO \"%w\".\"%w_patch\" VALUES(?,?,?,?,?)"
    " ON CONFLICT(col,block,slot) DO UPDATE SET value=excluded.value",t->schema,t->name));
  if( !rc ) rc=csPrepare(t,&remove,sqlite3_mprintf(
    "DELETE FROM \"%w\".\"%w_patch\" WHERE col=? AND block=? AND slot=?",t->schema,t->name));
  if( !rc ) rc=csPrepare(t,&count,sqlite3_mprintf(
    "SELECT count(*) FROM \"%w\".\"%w_patch\" WHERE col=? AND block=?",t->schema,t->name));
  for(i=0;!rc && i<t->ncol;i++) if( changed&((CsUnsigned)1<<i) ){
    CsCell base;
    sqlite3_stmt *s;
    memset(&base,0,sizeof(base));
    sqlite3_bind_int(original,1,i); sqlite3_bind_int64(original,2,block); sqlite3_bind_int(original,3,slot);
    rc=sqlite3_step(original);
    if( rc==SQLITE_ROW ) rc=csFromValue(t,&base,sqlite3_column_value(original,0),t->types[i]);
    else if( rc==SQLITE_DONE ){
      sqlite3_stmt *column=0;
      if( !b ) rc=csReadBlock(t,block,0,&b); else rc=SQLITE_OK;
      if( !rc && !csBit(b->live,slot) ) rc=SQLITE_CORRUPT_VTAB;
      if( !rc ) rc=csColumnStatement(t,&column);
      if( !rc ) rc=csLoadColumn(t,b,i,column);
      sqlite3_finalize(column);
      if( !rc ) rc=csCellCopy(&base,&b->cells[i][slot]);
    }
    sqlite3_reset(original);
    if( rc ){ csCellClear(&base); break; }
    s=csCellEqual(&base,&incoming[i]) ? remove : put;
    sqlite3_bind_int(s,1,i); sqlite3_bind_int64(s,2,block); sqlite3_bind_int(s,3,slot);
    if( s==put ){
      rc=csBind(s,4,&incoming[i]);
      if( !rc ) rc=csBind(s,5,&base);
    }
    csCellClear(&base);
    if( !rc ) rc=sqlite3_step(s);
    if( rc==SQLITE_DONE ) rc=sqlite3_reset(s);
    sqlite3_clear_bindings(s);
    if( rc ) break;
    t->patchesWritten++;
    sqlite3_bind_int(count,1,i); sqlite3_bind_int64(count,2,block);
    rc=sqlite3_step(count);
    if( rc==SQLITE_ROW ){
      int n=sqlite3_column_int(count,0);
      sqlite3_reset(count); rc=SQLITE_OK;
      /* Bound read amplification and pay dense merging in the writing transaction. */
      if( n>=64 ) rc=csCompactColumn(t,block,i);
    }
  }
  csFreeBlock(t,b);
  sqlite3_finalize(original); sqlite3_finalize(put); sqlite3_finalize(remove); sqlite3_finalize(count);
  return rc;
}
