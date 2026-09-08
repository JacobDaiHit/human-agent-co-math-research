import { createRoot } from 'react-dom/client'
import '@xyflow/react/dist/style.css'
import 'katex/dist/katex.min.css'
import './style.css'
import './workspace.css'
import { Workbench } from './Workbench'

createRoot(document.getElementById('root')!).render(<Workbench />)
