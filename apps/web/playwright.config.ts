import { defineConfig } from '@playwright/test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'

const data=mkdtempSync(join(tmpdir(),'mathagent-browser-'))
export default defineConfig({
  testDir:'./e2e',fullyParallel:false,workers:1,timeout:45000,
  expect:{timeout:10000},retries:0,
  reporter:[['list'],['json',{outputFile:'test-results/results.json'}]],
  use:{baseURL:'http://127.0.0.1:18081',viewport:{width:1440,height:1000},
    channel:process.env.MATHAGENT_TEST_BROWSER||'chrome',headless:true,
    trace:'retain-on-failure',screenshot:'only-on-failure'},
  webServer:{
    command:`"${resolve('../../.venv/Scripts/python.exe')}" -m uvicorn mathagent.api.app:create_app --factory --host 127.0.0.1 --port 18081 --no-access-log`,
    url:'http://127.0.0.1:18081/health',reuseExistingServer:false,timeout:30000,
    env:{MATHAGENT_DATABASE:join(data,'browser.sqlite3'),MATHAGENT_TOKEN:'browser-test-user',MATHAGENT_WORKER_TOKEN:'browser-test-worker',MATHAGENT_LOAD_ENV:'0',MATHAGENT_ENABLE_REAL_API:'0',MATHAGENT_DEEPSEEK_API_KEY:'',MATHAGENT_GLM_API_KEY:'',MATHAGENT_FRONTEND_ORIGIN:'http://127.0.0.1:18081'},
  },
})
