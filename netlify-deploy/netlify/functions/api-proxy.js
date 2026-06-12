// Netlify Function proxy for ATOS Dashboard API
// Routes /api -> Cloudflare Tunnel
const https = require('https');

// Hard-code the current tunnel URL
// This gets updated when the tunnel URL changes
const TUNNEL_HOST = 'tariff-explains-heath-oil.trycloudflare.com';

exports.handler = async (event, context) => {
  const path = '/api';
  
  return new Promise((resolve) => {
    const req = https.get({
      hostname: TUNNEL_HOST,
      path: path,
      headers: { 'Accept': 'application/json', 'User-Agent': 'Netlify-ATOS' },
      timeout: 20000,
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        resolve({
          statusCode: 200,
          headers: { 
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'no-cache',
          },
          body: data,
        });
      });
    });
    req.on('error', (e) => {
      resolve({
        statusCode: 502,
        body: JSON.stringify({ error: `Proxy error: ${e.message}` }),
      });
    });
    req.on('timeout', () => {
      req.destroy();
      resolve({
        statusCode: 504,
        body: JSON.stringify({ error: 'Tunnel timeout' }),
      });
    });
  });
};
