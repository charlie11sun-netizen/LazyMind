import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Button, Modal, Space } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DocumentPublicationRecoveryRequest, DocumentPublicationStatus } from '@/api/generated/core-client';
import { WorkflowSessionApi } from '@/modules/chat/utils/request';
import { documentPublicationUrl } from './documentPublicationUrl';
import './DocumentPublicationRecoveryPanel.scss';

const terminal = new Set(['succeeded','failed_no_write','canceled','outcome_unknown_released','confirmed_detached']);
export function DocumentPublicationRecoveryPanel({ artifactId, slotId, itemIndex, refreshKey, publishing, readOnly, canApplyLocal, onAvailability, onResolved, onPublished, onTarget }: {
  artifactId: string; slotId: string; itemIndex: number; refreshKey: number; publishing: boolean; readOnly?: boolean;
  canApplyLocal: () => boolean; onAvailability: (allowed: boolean) => void; onResolved: () => void;
  onPublished?: (url: string | undefined) => void;
  onTarget?: (url: string) => void;
}) {
  const {t}=useTranslation();
  const [operation,setOperation]=useState<DocumentPublicationStatus>();
  const [loaded,setLoaded]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [now,setNow]=useState(Date.now());
  const [expanded,setExpanded]=useState(false);
  const detailsId=useId();
  const alive=useRef(false),running=useRef(false),generation=useRef(0);
  const dialog=useRef<ReturnType<typeof Modal.confirm>>();
  const callbacks=useRef({canApplyLocal,onAvailability,onResolved,onPublished,onTarget});
  callbacks.current={canApplyLocal,onAvailability,onResolved,onPublished,onTarget};
  const silent={silentError:true};

  const load=useCallback(async()=>{
    const current=++generation.current;
    setBusy(true);setError('');
    try {
      const response=await WorkflowSessionApi().getPublicationForArtifact(artifactId,{silentError:true});
      if(alive.current && current===generation.current){setOperation(response.data.data.operation);setLoaded(true);setNow(Date.now());}
    } catch {
      if(alive.current && current===generation.current){setLoaded(false);setError('loadFailed');}
    } finally {if(alive.current && current===generation.current)setBusy(false);}
  },[artifactId]);
  useEffect(()=>{
    alive.current=true;
    return ()=>{alive.current=false;generation.current++;dialog.current?.destroy();};
  },[]);
  useEffect(()=>{void load();},[load,refreshKey]);
  useEffect(()=>{
    callbacks.current.onAvailability(loaded && !busy && !error && (!operation || terminal.has(operation.status)));
  },[loaded,busy,error,operation]);
  useEffect(()=>{
    if(loaded && !error && operation && operation.source_slot_id===slotId && operation.item_index===itemIndex) {
      const url=documentPublicationUrl(operation.target_url);
      if(url)callbacks.current.onTarget?.(url);
      if(operation.status==='succeeded')callbacks.current.onPublished?.(url);
    }
  },[loaded,error,operation,slotId,itemIndex]);
  useEffect(()=>{
    if(operation?.status!=='write_started' || !operation.recovery_after) return;
    const delay=new Date(operation.recovery_after).getTime()-Date.now();
    if(delay<=0)return;
    const timer=window.setTimeout(()=>setNow(Date.now()),Math.min(delay+20,2147483647));
    return ()=>window.clearTimeout(timer);
  },[operation]);

  const run=async(action:'cancel'|'retry_local'|DocumentPublicationRecoveryRequest['action'],reason?:DocumentPublicationRecoveryRequest['reason'])=>{
    if(!operation || running.current || publishing || readOnly || !alive.current)return;
    if(action==='retry_local' && !callbacks.current.canApplyLocal()){setError('unsaved');return;}
    running.current=true;setBusy(true);setError('');
    const current=++generation.current;
    try {
      const api=WorkflowSessionApi();
      let status:DocumentPublicationStatus;
      if(action==='retry_local') {
        await api.retryPublicationLocal(operation.operation_id,silent);
        status=(await api.readPublication(operation.operation_id,silent)).data.data;
      } else if(action==='cancel') status=(await api.cancelPublication(operation.operation_id,silent)).data.data;
      else status=(await api.recoverPublication(operation.operation_id,{action,confirmed:action!=='check',reason},silent)).data.data;
      if(alive.current && current===generation.current){
        setOperation(status);setLoaded(true);setNow(Date.now());
        if(terminal.has(status.status))callbacks.current.onResolved();
      }
    } catch (failure) {
      const code=(failure as {response?:{data?:{data?:{code?:string}}}})?.response?.data?.data?.code;
      if(alive.current && current===generation.current)setError(code==='PROVIDER_SYNC_LOCAL_CONFLICT'?'localConflict':'actionFailed');
    } finally {running.current=false;if(alive.current && current===generation.current)setBusy(false);}
  };
  const confirm=(action:'release_unknown'|'keep_remote',reason?:DocumentPublicationRecoveryRequest['reason'])=>{
    const message=action==='keep_remote'?'keepRemoteWarning':reason==='user_verified_no_write'?'verifiedWarning':'unknownWarning';
    dialog.current=Modal.confirm({title:t(`chat.writerIR.publicationRecovery.${action==='keep_remote'?'keepRemote':'release'}`),
      content:t(`chat.writerIR.publicationRecovery.${message}`),okText:t('chat.writerIR.publicationRecovery.confirm'),cancelText:t('chat.writerIR.publicationRecovery.cancelDialog'),
      onOk:()=>run(action,reason)});
  };
  const label=(key:string)=>t(`chat.writerIR.publicationRecovery.${key}`);
  const disabled=busy || publishing || readOnly || (Boolean(error) && !['unsaved','localConflict'].includes(error));
  const actions=operation?.actions ?? [];
  const expired=operation?.status==='write_started' && operation.recovery_after && new Date(operation.recovery_after).getTime()<=now;
  if(!loaded && !error)return null;
  if(!operation && !error)return null;
  const target=documentPublicationUrl(operation?.target_url);
  const hasFooterTarget=Boolean(onTarget && operation?.source_slot_id===slotId && operation?.item_index===itemIndex);
  if(operation && terminal.has(operation.status) && operation.status!=='succeeded' && !error) {
    return target && !hasFooterTarget ? <a className='document-publication-recovery__link' href={target} target='_blank' rel='noreferrer'>{t('chat.writerIR.openCloudDocument')}</a> : null;
  }
  if(operation?.status==='succeeded' && !error) {
    const needsRefresh=operation.artifact_id && operation.artifact_id!==artifactId && operation.source_slot_id===slotId && operation.item_index===itemIndex;
    if(onPublished && !needsRefresh)return null;
    return <Space>
      {!onPublished && (target ? <a href={target} target='_blank' rel='noreferrer'>{t('chat.writerIR.openCloudDocument')}</a> : <span>{t('chat.writerIR.writeBackSuccess')}</span>)}
      {needsRefresh && <Button size='small' type='link' disabled={disabled} onClick={()=>callbacks.current.onResolved()}>{label('updateDraft')}</Button>}
    </Space>;
  }
  const known=operation && ['preparing','write_started','outcome_unknown','provider_confirmed','local_conflict','local_persist_failed','succeeded','outcome_unknown_released','confirmed_detached'].includes(operation.status);
  return <section className='document-publication-recovery'>
    <button type='button' className='document-publication-recovery__toggle' aria-expanded={expanded} aria-controls={detailsId} onClick={()=>setExpanded(value=>!value)}>
      <span aria-hidden>{expanded?'▾':'▸'}</span>{label('status')}
    </button>
    {expanded && <div id={detailsId} className='document-publication-recovery__details'><Space direction='vertical' style={{width:'100%'}}>
      {operation && <>
        <span>{label(known?`states.${operation.status}`:'unknownState')}</span>
        {(operation.source_slot_id!==slotId || operation.item_index!==itemIndex) && <span>{label('otherDraft')}</span>}
        {target?<a href={target} target='_blank' rel='noreferrer'>{label('openTarget')}</a>:<span>{label('noTarget')}</span>}
        <details><summary>{label('details')}</summary><div style={{overflowWrap:'anywhere'}}>{operation.operation_id}</div><time dateTime={operation.updated_at}>{new Date(operation.updated_at).toLocaleString()}</time></details>
      </>}
      {error && <span role='alert'>{label(error)}</span>}
      <Space wrap size={6}>
        <Button disabled={busy || publishing} onClick={()=>void load()}>{label(error==='loadFailed'?'retryQuery':'refresh')}</Button>
        {actions.includes('cancel') && <Button disabled={disabled} onClick={()=>void run('cancel')}>{label('cancel')}</Button>}
        {(actions.includes('check') || expired) && <Button disabled={disabled} onClick={()=>void run('check')}>{label('check')}</Button>}
        {actions.includes('retry_local') && <Button disabled={disabled || error==='unsaved' || error==='localConflict'} onClick={()=>void run('retry_local')}>{label('retryLocal')}</Button>}
        {actions.includes('keep_remote') && <Button disabled={disabled} onClick={()=>confirm('keep_remote')}>{label('keepRemote')}</Button>}
        {actions.includes('release_unknown') && <>
          <Button disabled={disabled} onClick={()=>confirm('release_unknown','user_verified_no_write')}>{label('verifiedRelease')}</Button>
          <Button disabled={disabled} onClick={()=>confirm('release_unknown','accept_unknown')}>{label('release')}</Button>
        </>}
      </Space>
    </Space></div>}
  </section>;
}
