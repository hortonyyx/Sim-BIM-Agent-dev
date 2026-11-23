const copy = {
  settings: ['模型设置', 'Model settings'], language: ['语言', 'Language'],
  title: ['建模工作台', 'Modeling workspace'], configuration: ['任务配置', 'Task configuration'],
  workspace: ['工作区', 'Workspace'], chooseFolder: ['选择文件夹', 'Choose folder'],
  coordinator: ['主控模型', 'Orchestrator'], workers: ['协作模型', 'Workers'],
  chooseModel: ['选择模型', 'Select a model'], noWorkers: ['不使用协作模型', 'No workers'],
  blockNew: ['新建素材组', 'New block'], blockEdit: ['编辑素材组', 'Edit block'],
  block: ['素材组', 'Block'], cancelEdit: ['取消编辑', 'Cancel editing'],
  drop: ['将素材文件拖到这里', 'Drop source files here'], chooseFiles: ['选择文件', 'Choose files'],
  caption: ['素材组说明', 'Block caption'], optional: ['可留空', 'Optional'],
  captionHint: ['例如：三张平面图依次对应底层、标准层和顶层。', 'For example, these plans show the ground, typical and top floors, in that order.'],
  add: ['添加到素材栏', 'Add to library'], saveBlock: ['保存素材组', 'Save block'],
  library: ['素材栏', 'Material library'], empty: ['暂无素材组', 'No blocks yet'],
  brief: ['总任务说明', 'Task brief'], run: ['开始建模', 'Run model'],
  briefDefault: ['根据提供的建筑资料生成可查看的完整轻量 BIM。保留可观察的楼层、空间、边界、开口和连接，注明推断及尚未解决的问题。', 'Generate a complete, viewable lightweight BIM from the supplied building materials. Preserve the observable floors, spaces, boundaries, openings and connections. Identify assumptions and unresolved questions.'],
  noWorkspace: ['先选择保存素材和结果的工作区。', 'Choose a workspace for source files and results.'],
  noBlocks: ['添加素材组后即可提交任务。', 'Add a block to prepare the task.'],
  noCoordinator: ['请选择本次任务的主控模型。', 'Choose an orchestrator for this run.'],
  pendingBlock: ['请先保存当前素材组，或取消编辑。', 'Save the current block or cancel the edit first.'],
  missingSetup: ['在模型设置中连接账号并获取可用模型。', 'Connect an account in Model settings to get available models.'],
  emptyBlock: ['请先选择文件或填写素材组说明。', 'Choose a file or enter a block caption first.'],
  discard: ['放弃尚未保存的素材组修改？', 'Discard the unsaved changes to this block?'],
  removeConfirm: ['从本次任务中移除此素材组？', 'Remove this block from the task?'],
  path: ['工作区路径', 'Workspace path'], pathHint: ['输入本机文件夹的完整路径', 'Enter the full path to a local folder'],
  systemBrowse: ['浏览文件夹…', 'Browse folders…'], useFolder: ['使用此文件夹', 'Use this folder'],
  connected: ['已连接', 'Connected'], disconnected: ['未连接', 'Disconnected'],
  connect: ['登录 ChatGPT', 'Sign in to ChatGPT'], refresh: ['获取模型列表', 'Fetch models'],
  configure: ['保存密钥', 'Save key'], replaceKey: ['替换 API 密钥', 'Replace API key'],
  key: ['API 密钥', 'API key'], usage: ['DeepSeek API 按用量计费。', 'DeepSeek API is billed by usage.'],
  statusPending: ['正在处理…', 'Working…'], settingsError: ['模型连接失败。', 'Model connection failed.'],
  loginLaunched: ['请在打开的登录页完成授权，然后获取模型列表。', 'Finish signing in in the opened window, then fetch models.'],
  done: ['完成', 'Done'], close: ['关闭', 'Close'], cancel: ['取消', 'Cancel'],
  file: ['文件', 'File'], edit: ['编辑', 'Edit'], remove: ['移除', 'Remove'],
  moveUp: ['上移', 'Move up'], moveDown: ['下移', 'Move down'], preview: ['预览', 'Preview'],
  blockedFormat: ['此工作台支持 PNG 图像、GLB 网格和建筑说明 JSON。', 'This workbench accepts PNG images, GLB meshes and building-declaration JSON.'],
  unsupportedFiles: ['未添加的文件', 'Files not added'], noKey: ['请先输入 API 密钥。', 'Enter the API key first.'],
  completed: ['建模完成', 'Model complete'], failed: ['建模失败', 'Run failed'],
  running: ['建模进行中', 'Model running'], previousRunning: ['上一版任务正在建模', 'An earlier draft is running'],
  viewer: ['打开模型查看器', 'Open model viewer'],
  report: ['查看报告', 'View report'], error: ['操作失败', 'Action failed'],
  assetsChanged: ['工作区内容已变化。已载入最新版本，请检查后再试。', 'The workspace has changed. Review the latest version and try again.'],
  previewError: ['无法预览此文件。', 'This file cannot be previewed.'], load: ['正在读取文件…', 'Reading file…'],
  meshHint: ['拖动旋转，滚动缩放。', 'Drag to orbit. Scroll to zoom.'],
  meshPositions: ['可读取的顶点', 'Readable vertices'], meshPreview: ['网格几何预览', 'Mesh geometry preview'],
  diagnostic: ['技术详情', 'Technical details'], selectionMissing: ['当前选择暂不可用', 'Current selection unavailable'],
  workspaceNeedsVisual: ['请添加至少一张 PNG 图像或一个 GLB 网格。', 'Add at least one PNG image or GLB mesh.'],
  saveInProgress: ['正在保存…', 'Saving…'], refreshPage: ['重新读取工作区', 'Reload workspace'],
  browserOffline: ['尚未连接本地工作台，请重新载入。', 'The local workbench is unavailable. Reload to reconnect.'],
  noModelsFound: ['暂未获取到模型。', 'No models have been fetched yet.']
};
export const tr = (lang, key) => copy[key]?.[lang === 'en' ? 1 : 0] ?? key;
export function totals(lang, blocks, files) {
  return lang === 'en' ? blocks + (blocks === 1 ? ' block · ' : ' blocks · ') + files + (files === 1 ? ' file' : ' files')
    : blocks + ' 组 · ' + files + ' 个文件';
}
export function catalogCount(lang, count) {
  return lang === 'en' ? count + (count === 1 ? ' available model' : ' available models') : count + ' 个可用模型';
}
