"""Customer-facing invoice PDF; internal costs and profit are never rendered."""
import io
import sys
import os
import threading
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0,str(Path(__file__).resolve().parent/'.deps'))
FONT_LOCK=threading.Lock()


def render(invoice):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with FONT_LOCK:
        if 'InvoiceFont' not in pdfmetrics.getRegisteredFontNames():
            font_path=Path(os.environ.get('SHIPMENT_INVOICE_FONT',str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'msjh.ttc')))
            if not font_path.is_file(): raise ValueError('Invoice font is unavailable. Configure SHIPMENT_INVOICE_FONT with a Unicode TrueType font path.')
            pdfmetrics.registerFont(TTFont('InvoiceFont',str(font_path),subfontIndex=0))
    out=io.BytesIO(); s=invoice['snapshot']; c=s['company']; draft=invoice['status']=='Draft'
    credit=invoice.get('document_type')=='Credit note'
    label='Credit note' if credit else 'Invoice'
    supported=pdfmetrics.getFont('InvoiceFont').face.charToGlyph
    def validate(value):
        if isinstance(value,dict):
            for item in value.values(): validate(item)
        elif isinstance(value,list):
            for item in value: validate(item)
        elif isinstance(value,str) and any(ord(ch) not in supported for ch in value if ch not in '\n\r\t'):
            raise ValueError('The invoice contains characters unsupported by its PDF font. Use a compatible Unicode font or revise the text.')
    validate(s)
    styles=getSampleStyleSheet()
    body=ParagraphStyle('InvoiceBody',fontName='InvoiceFont',fontSize=10,leading=15,spaceAfter=6,textColor=colors.HexColor('#20384e'))
    heading=ParagraphStyle('InvoiceHeading',parent=body,fontSize=23,leading=29,spaceAfter=16)
    right=ParagraphStyle('InvoiceRight',parent=body,alignment=TA_RIGHT)
    p=lambda text,style=body:Paragraph(escape(str(text)).replace('\n','<br/>'),style)
    cash=lambda n:f"{s['currency']} {float(n):,.2f}"
    number=invoice.get('number') or 'Unnumbered draft'
    doc=SimpleDocTemplate(out,pagesize=(595.28,841.89),rightMargin=44,leftMargin=44,topMargin=48,bottomMargin=48,
                          title=('DRAFT ' if draft else '')+label+' '+number,author=c['company'])
    story=[p('CREDIT NOTE' if credit else 'DRAFT INVOICE — NOT ISSUED' if draft else 'INVOICE',heading),p(c['company'],heading),p(c['address'])]
    if c['tax_id']:story.append(p('Tax / registration ID: '+c['tax_id']))
    if c['email']:story.append(p(c['email']))
    story += [Spacer(1,14),p(f"{label}: {number}   |   Shipment: {s['reference']}"),p(f"Credit date: {s['issue_date']}" if credit else f"Invoice date: {s['issue_date']}   |   Due date: {s['due_date']}"),Spacer(1,12),p('Credit to' if credit else 'Bill to',heading),p(s['customer']),p(s['customer_address'] or 'Billing address not yet provided'),p(s['email'])]
    if s['customer_tax_id']:story.append(p('Customer tax / registration ID: '+s['customer_tax_id']))
    story.append(Spacer(1,16))
    rows=[[p('Description'),p('Amount',right)]]+[[p(line['label']),p(cash(line['amount']),right)] for line in s['lines']]
    table=Table(rows,colWidths=[350,157],repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf1f8')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor('#cbd5e1'))]))
    story += [table,Spacer(1,15),p('Net amount: '+cash(s['net']),right),p(c['tax_label']+' ('+c['tax_rate']+'%): '+cash(s['tax']),right),p(('Total credited: ' if credit else 'Total due: ')+cash(s['total']),right)]
    mode={'none':'No tax added.','exclusive':'Configured tax is added to the approved charges.','inclusive':'Configured tax is included in the approved charges.'}[c['tax_mode']]
    story += [p(mode),Spacer(1,14),p('Payment terms'),p(c['terms'])]
    if c['bank']:story += [p('Payment instructions'),p(c['bank'])]
    if s['notes']:story += [p('Credit note reason' if credit else 'Invoice notes'),p(s['notes'])]
    def footer(canvas,document):
        canvas.saveState();canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#64748b'))
        canvas.drawString(44,26,('DRAFT - NOT ISSUED' if draft else number));canvas.drawRightString(551,26,f'Page {document.page}');canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()
