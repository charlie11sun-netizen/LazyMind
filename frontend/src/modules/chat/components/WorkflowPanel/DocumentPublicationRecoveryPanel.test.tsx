import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { DocumentPublicationRecoveryPanel } from './DocumentPublicationRecoveryPanel';
const api=vi.hoisted(()=>({getPublicationForArtifact:vi.fn(),readPublication:vi.fn(),recoverPublication:vi.fn(),cancelPublication:vi.fn(),retryPublicationLocal:vi.fn(),publishDocument:vi.fn()}));
const confirm=vi.hoisted(()=>vi.fn());
vi.mock('@/modules/chat/utils/request',()=>({WorkflowSessionApi:()=>api}));
vi.mock('antd',async(importOriginal)=>({...await importOriginal<typeof import('antd')>(),Modal:{confirm}}));
const unknown={operation_id:'op-fixture',status:'outcome_unknown',provider:'feishu',provider_synced:false,updated_at:'2026-09-14T00:00:00Z',actions:['release_unknown'],source_slot_id:'draft_document',item_index:-1};
const response=(operation:unknown)=>({data:{data:{operation}}});
function setup(canApplyLocal=()=>true){
 const onResolved=vi.fn(),onAvailability=vi.fn();
 const rendered=render(<DocumentPublicationRecoveryPanel artifactId='artifact-fixture' slotId='draft_document' itemIndex={-1} refreshKey={0} publishing={false} canApplyLocal={canApplyLocal} onResolved={onResolved} onAvailability={onAvailability}/>);
 return {...rendered,onResolved,onAvailability};
}
async function expandRecovery() {
 fireEvent.click(await screen.findByRole('button',{name:'发布状态'}));
}
beforeEach(async()=>{
 vi.resetAllMocks();await i18n.changeLanguage('zh-CN');
 confirm.mockReturnValue({destroy:vi.fn()});
 api.getPublicationForArtifact.mockResolvedValue(response(unknown));
});
it('rediscovers the durable operation after remount instead of replaying a publication',async()=>{
 const first=setup();await expandRecovery();await screen.findByRole('button',{name:'已核对未写入，解除占用'});first.unmount();
 setup();await expandRecovery();await screen.findByRole('button',{name:'已核对未写入，解除占用'});
 expect(api.getPublicationForArtifact).toHaveBeenCalledTimes(2);
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it('requires explicit risk confirmation, then releases without automatically publishing',async()=>{
 const {onResolved,onAvailability}=setup();
 await expandRecovery();
 fireEvent.click(await screen.findByRole('button',{name:'结束跟踪并解除占用'}));
 expect(confirm.mock.calls[0][0].content).toContain('可能重复创建或覆盖');
 expect(api.recoverPublication).not.toHaveBeenCalled();
 api.recoverPublication.mockResolvedValue({data:{data:{...unknown,status:'outcome_unknown_released',actions:[]}}});
 await act(async()=>confirm.mock.calls[0][0].onOk());
 expect(api.recoverPublication).toHaveBeenCalledWith('op-fixture',{action:'release_unknown',confirmed:true,reason:'accept_unknown'},expect.anything());
 expect(onResolved).toHaveBeenCalledTimes(1);
 await waitFor(()=>expect(onAvailability).toHaveBeenLastCalledWith(true));
 expect(api.publishDocument).not.toHaveBeenCalled();
 expect(screen.queryByRole('button',{name:'结束跟踪并解除占用'})).not.toBeInTheDocument();
});
it('does not execute a dismissed confirmation or a stale dialog after unmount',async()=>{
 const rendered=setup();
 await expandRecovery();
 fireEvent.click(await screen.findByRole('button',{name:'已核对未写入，解除占用'}));
 expect(api.recoverPublication).not.toHaveBeenCalled();
 rendered.unmount();await act(async()=>confirm.mock.calls[0][0].onOk());
 expect(api.recoverPublication).not.toHaveBeenCalled();
});
it('preserves local edits and permits keeping a confirmed remote result separately',async()=>{
 const confirmed={...unknown,status:'local_conflict',provider_synced:true,actions:['retry_local','keep_remote']};
 api.getPublicationForArtifact.mockResolvedValue(response(confirmed));
 setup(()=>false);
 await expandRecovery();
 fireEvent.click(await screen.findByRole('button',{name:'补存发布结果'}));
 expect(await screen.findByText('当前有未保存修改，不能补存覆盖。请先保存修改，或保留云端结果并解除占用。')).toBeVisible();
 expect(api.retryPublicationLocal).not.toHaveBeenCalled();
 expect(screen.getByRole('button',{name:'保留云端结果并解除占用'})).toBeEnabled();
 fireEvent.click(screen.getByRole('button',{name:'保留云端结果并解除占用'}));
 expect(confirm.mock.calls[0][0].content).toContain('不覆盖当前本地稿');
});
it('provides retry after lookup failure and does not enable publication until status is known',async()=>{
 api.getPublicationForArtifact.mockRejectedValueOnce(new Error('offline'));
 const {onAvailability}=setup();
 await expandRecovery();
 expect(await screen.findByRole('button',{name:'重试查询'})).toBeEnabled();
 expect(onAvailability).toHaveBeenLastCalledWith(false);
 api.getPublicationForArtifact.mockResolvedValue(response(undefined));
 fireEvent.click(screen.getByRole('button',{name:'重试查询'}));
 await waitFor(()=>expect(onAvailability).toHaveBeenLastCalledWith(true));
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it('uses only local retry and a status read for a stored success receipt',async()=>{
 const confirmed={...unknown,status:'provider_confirmed',provider_synced:true,actions:['retry_local','keep_remote']};
 api.getPublicationForArtifact.mockResolvedValue(response(confirmed));
 api.retryPublicationLocal.mockResolvedValue({data:{data:{artifact_id:'saved-artifact'}}});
 api.readPublication.mockResolvedValue({data:{data:{...confirmed,status:'succeeded',artifact_id:'saved-artifact',actions:[]}}});
 const {onResolved}=setup();
 await expandRecovery();
 fireEvent.click(await screen.findByRole('button',{name:'补存发布结果'}));
 await waitFor(()=>expect(onResolved).toHaveBeenCalledTimes(1));
 expect(api.retryPublicationLocal).toHaveBeenCalledTimes(1);
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it('keeps an unrecognized lookup state blocked',async()=>{
 api.getPublicationForArtifact.mockResolvedValue(response({...unknown,status:'unrecognized-state',actions:[]}));
 const {onAvailability,onResolved}=setup();
 await expandRecovery();
 await screen.findByText('暂时无法识别发布状态，请刷新状态后再处理。');
 expect(onAvailability).toHaveBeenLastCalledWith(false);
 expect(onResolved).not.toHaveBeenCalled();
});
it('does not resolve or enable publication after an unrecognized recovery response',async()=>{
 api.recoverPublication.mockResolvedValue({data:{data:{...unknown,status:'unrecognized-state',actions:[]}}});
 const {onAvailability,onResolved}=setup();
 await expandRecovery();
 fireEvent.click(await screen.findByRole('button',{name:'结束跟踪并解除占用'}));
 await act(async()=>confirm.mock.calls[0][0].onOk());
 expect(onAvailability).toHaveBeenLastCalledWith(false);
 expect(onResolved).not.toHaveBeenCalled();
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it('builds the generated lookup request from only an artifact ID',async()=>{
 const {WorkflowApiAxiosParamCreator}=await import('@/api/generated/core-client');
 const request=await WorkflowApiAxiosParamCreator().apiCoreWorkflowArtifactsArtifactIdPublicationGet('artifact-fixture');
 expect(request.url).toBe('/api/core/workflow-artifacts/artifact-fixture/publication');
  expect(request.options.method).toBe('GET');
});
it('keeps a compact document link available for read-only success without a footer handler',async()=>{
 api.getPublicationForArtifact.mockResolvedValue(response({...unknown,status:'succeeded',provider_synced:true,artifact_id:'artifact-fixture',target_url:'https://example.test/published-document',actions:[]}));
 render(<DocumentPublicationRecoveryPanel artifactId='artifact-fixture' slotId='draft_document' itemIndex={-1} refreshKey={0} publishing={false} readOnly canApplyLocal={()=>false} onResolved={vi.fn()} onAvailability={vi.fn()}/>);
 expect(await screen.findByRole('link',{name:'打开云文档'})).toHaveAttribute('href','https://example.test/published-document');
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
 expect(screen.queryByText('操作信息')).not.toBeInTheDocument();
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it.each(['outcome_unknown_released','confirmed_detached','failed_no_write','canceled'])('hides ended publication notices and keeps a known target link: %s',async status=>{
 api.getPublicationForArtifact.mockResolvedValue(response({...unknown,status,target_url:'https://example.test/document',actions:[]}));
 const {onAvailability}=setup();
 await waitFor(()=>expect(onAvailability).toHaveBeenLastCalledWith(true));
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
 expect(screen.queryByText('上次云文档发布')).not.toBeInTheDocument();
 expect(screen.queryByText('操作信息')).not.toBeInTheDocument();
 expect(screen.queryByRole('button',{name:'发布状态'})).not.toBeInTheDocument();
 expect(screen.getByRole('link',{name:'打开云文档'})).toHaveAttribute('href','https://example.test/document');
 expect(api.publishDocument).not.toHaveBeenCalled();
});
it.each(['preparing','write_started','outcome_unknown','provider_confirmed','local_conflict','local_persist_failed','unrecognized-state'])('keeps recovery details collapsed until requested: %s',async status=>{
 api.getPublicationForArtifact.mockResolvedValue(response({...unknown,status,actions:[]}));
 const {onAvailability}=setup();
 const toggle=await screen.findByRole('button',{name:'发布状态'});
 expect(toggle).toHaveAttribute('aria-expanded','false');
 expect(screen.queryByText('操作信息')).not.toBeInTheDocument();
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
 expect(onAvailability).toHaveBeenLastCalledWith(false);
 fireEvent.click(toggle);
 expect(toggle).toHaveAttribute('aria-expanded','true');
 expect(await screen.findByRole('button',{name:'刷新状态'})).toBeEnabled();
 fireEvent.click(toggle);
 expect(screen.queryByRole('button',{name:'刷新状态'})).not.toBeInTheDocument();
 expect(api.publishDocument).not.toHaveBeenCalled();
});
