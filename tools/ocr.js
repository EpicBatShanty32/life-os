ObjC.import('Cocoa');
ObjC.import('Vision');

function ocr(path) {
  const url = $.NSURL.fileURLWithPath($(path));
  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $());
  const req = $.VNRecognizeTextRequest.alloc.init;
  req.recognitionLevel = 1; // accurate
  req.usesLanguageCorrection = true;
  req.recognitionLanguages = $(['ru-RU', 'en-US']);
  req.customWords = $(['Obsidian', 'Telegram', 'Дашборд']);
  let err = $();
  handler.performRequestsError($([req]), err);
  const res = req.results;
  const lines = [];
  for (let i = 0; i < res.count; i++) {
    const obs = res.objectAtIndex(i);
    const cands = obs.topCandidates(1);
    if (cands.count === 0) continue;
    const s = cands.objectAtIndex(0).string.js;
    const bb = obs.boundingBox;
    // normalized bbox, origin bottom-left -> invert y to top-left
    lines.push({
      t: s,
      x: Math.round(bb.origin.x * 1000) / 1000,
      y: Math.round((1 - bb.origin.y - bb.size.height) * 1000) / 1000,
      w: Math.round(bb.size.width * 1000) / 1000,
      h: Math.round(bb.size.height * 1000) / 1000,
    });
  }
  return lines;
}

function run(argv) {
  const out = {};
  for (const p of argv) out[p] = ocr(p);
  return JSON.stringify(out);
}
