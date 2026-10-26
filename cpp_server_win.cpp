// HTTP server and job manager. CT simulation runs through embedded Python.
#include <Python.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <regex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <basetsd.h>
#pragma comment(lib, "ws2_32.lib")
typedef SSIZE_T ssize_t;
static inline void close_socket(int fd) { closesocket(static_cast<SOCKET>(fd)); }
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
static inline void close_socket(int fd) { ::close(fd); }
#endif

namespace fs = std::filesystem;
namespace {
constexpr size_t kMaxBody = 512ULL * 1024 * 1024;
constexpr size_t kMaxJobsKept = 3;
std::atomic<bool> server_running{true};
std::atomic<uint64_t> id_counter{0};
std::mutex python_mutex;
std::mutex jobs_mutex;
std::unordered_map<std::string, std::shared_ptr<struct Job>> jobs;
PyObject* engine_module = nullptr;
std::string page_html;

std::string object_slot_html(const std::string& i,bool is_open=false) {
  std::string html="\n<details class=\"object-slot\" ";
  if(is_open) html+="open";
  html+=">\n  <summary>Object <span class=\"object-number\">"+i+"</span></summary>\n"
    "  <label>Mesh file (STL, OBJ, PLY, or DAE) <input type=\"file\" name=\"file"+i+"\" accept=\".stl,.obj,.ply,.dae\"></label>\n"
    "  <div class=\"row\">\n    <label>Name <input name=\"name"+i+"\" value=\"part"+i+"\"></label>\n"
    "    <label>Material type <select name=\"kind"+i+"\">\n      <option>Element</option><option selected>Compound</option><option>Mixture</option>\n    </select></label>\n  </div>\n"
    "  <div class=\"row\">\n    <label>Material <input name=\"material"+i+"\" value=\"H2O\"></label>\n"
    "    <label>Density g/cm&sup3; <input name=\"density"+i+"\" type=\"number\" step=\"any\" value=\"1.0\"></label>\n  </div>\n"
    "  <div class=\"hint\">Element: Fe or 26 &middot; Compound: H2O &middot; Mixture: Ti90Al6V4</div>\n"
    "  <div class=\"row\">\n    <label>Offset x mm <input name=\"ox"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n"
    "    <label>Offset y mm <input name=\"oy"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n"
    "    <label>Offset z mm <input name=\"oz"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n  </div>\n"
    "  <div class=\"orient\">\n    <strong>Orientation</strong> (degrees, turns the object before the scan)\n    <div class=\"row\">\n"
    "      <label>Rotate x <input name=\"rx"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n"
    "      <label>Rotate y <input name=\"ry"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n"
    "      <label>Rotate z <input name=\"rz"+i+"\" type=\"number\" step=\"any\" value=\"0\"></label>\n    </div>\n"
    "    <div class=\"presets\">\n      <button type=\"button\" onclick=\"setRot("+i+",0,0,0)\">As loaded</button>\n"
    "      <button type=\"button\" onclick=\"setRot("+i+",0,90,0)\">Tip y +90</button>\n"
    "      <button type=\"button\" onclick=\"setRot("+i+",0,-90,0)\">Tip y &minus;90</button>\n"
    "      <button type=\"button\" onclick=\"setRot("+i+",90,0,0)\">Spin x +90</button>\n"
    "      <button type=\"button\" onclick=\"setRot("+i+",0,0,90)\">Turn z +90</button>\n    </div>\n"
    "    <div class=\"hint\">x = along the beam (spins the picture), y = horizontal axis of the\n"
    "      image (tips the object over: side view &rarr; top or bottom view),\n"
    "      z = vertical axis of the image (turns it toward the front or back).</div>\n  </div>\n</details>";
  return html;
}
std::string replace_all(std::string text,const std::string& token,const std::string& replacement) {
  size_t at=0; while((at=text.find(token,at))!=std::string::npos) { text.replace(at,token.size(),replacement); at+=replacement.size(); }
  return text;
}
std::string page_slots() {
  return object_slot_html("1",true)+"\n<template id=\"object-template\">"+object_slot_html("__INDEX__")+"</template>";
}

struct UploadedPart { std::string filename, data; };
struct Request {
  std::string method, target, content_type, body;
  std::map<std::string, std::string> fields;
  std::map<std::string, UploadedPart> files;
};
struct Response {
  int status = 200;
  std::string type = "application/json; charset=utf-8";
  std::string body;
  std::string disposition;
  fs::path file_path;
};
struct Job {
  std::string id, kind="scan", status="running", message="Starting...", error;
  double progress=0, total_deg=0;
  int n=0;
  bool has_zip=false, has_sino_zip=false, has_batch_zip=false;
  std::vector<std::string> kinds;
  std::chrono::steady_clock::time_point created=std::chrono::steady_clock::now();
  PyObject* prepared=nullptr;
  PyObject* payload=nullptr;
  fs::path upload_dir;
  fs::path batch_dir;
};

void stop_server(int) {
  server_running = false;
#ifdef _WIN32
  // Windows accept() ignores Ctrl+C, so exit from the signal handler.
  std::_Exit(0);
#endif
}
std::string trim(std::string value) {
  const auto a=value.find_first_not_of(" \t\r\n");
  if(a==std::string::npos) return {};
  return value.substr(a,value.find_last_not_of(" \t\r\n")-a+1);
}
std::string lower(std::string value) {
  std::transform(value.begin(),value.end(),value.begin(),[](unsigned char c){return std::tolower(c);});
  return value;
}
std::string json_escape(const std::string& value) {
  std::ostringstream out;
  for(unsigned char c:value) {
    switch(c) {
      case '"': out<<"\\\""; break; case '\\': out<<"\\\\"; break;
      case '\b': out<<"\\b"; break; case '\f': out<<"\\f"; break;
      case '\n': out<<"\\n"; break; case '\r': out<<"\\r"; break; case '\t': out<<"\\t"; break;
      default: if(c<0x20) out<<"\\u"<<std::hex<<std::setw(4)<<std::setfill('0')<<int(c)<<std::dec; else out<<c;
    }
  }
  return out.str();
}
std::string json_error(const std::string& error) { return "{\"error\":\""+json_escape(error)+"\"}"; }
std::string url_decode(const std::string& value) {
  std::string out;
  for(size_t i=0;i<value.size();++i) {
    if(value[i]=='+') out.push_back(' ');
    else if(value[i]=='%' && i+2<value.size()) {
      try { out.push_back(static_cast<char>(std::stoi(value.substr(i+1,2),nullptr,16))); i+=2; }
      catch(...) { out.push_back(value[i]); }
    } else out.push_back(value[i]);
  }
  return out;
}

bool receive_exact(int fd, std::string& out, size_t count) {
  char buffer[16384];
  while(out.size()<count) {
    const size_t want=std::min(sizeof(buffer),count-out.size());
    const ssize_t got=recv(fd,buffer,want,0);
    if(got<=0) return false;
    out.append(buffer,static_cast<size_t>(got));
  }
  return true;
}
bool read_request(int fd, Request& req) {
  std::string raw; char ch;
  while(raw.size()<65536) {
    if(recv(fd,&ch,1,0)!=1) return false;
    raw.push_back(ch);
    if(raw.size()>=4 && raw.compare(raw.size()-4,4,"\r\n\r\n")==0) break;
  }
  const auto split=raw.find("\r\n\r\n");
  if(split==std::string::npos) return false;
  std::istringstream lines(raw.substr(0,split));
  std::string first; std::getline(lines,first);
  std::istringstream request_line(first); std::string version;
  if(!(request_line>>req.method>>req.target>>version)) return false;
  size_t length=0; std::string line;
  while(std::getline(lines,line)) {
    const auto colon=line.find(':'); if(colon==std::string::npos) continue;
    const auto key=lower(trim(line.substr(0,colon))); const auto value=trim(line.substr(colon+1));
    if(key=="content-length") { try { length=std::stoull(value); } catch(...) { return false; } }
    else if(key=="content-type") req.content_type=value;
    else if(key=="transfer-encoding" && lower(value).find("chunked")!=std::string::npos) return false;
  }
  if(length>kMaxBody) return false;
  req.body.assign(raw.data()+split+4,raw.size()-split-4);
  if(req.body.size()<length && !receive_exact(fd,req.body,length)) return false;
  if(req.body.size()>length) req.body.resize(length);
  return true;
}

std::string header_param(const std::string& header,const std::string& key) {
  std::regex re("(?:^|;)\\s*"+key+"\\s*=\\s*(?:\"([^\"]*)\"|([^;\\s]*))",std::regex::icase);
  std::smatch m;
  if(!std::regex_search(header,m,re)) return {};
  return m[1].matched?m[1].str():m[2].str();
}
void parse_body(Request& req) {
  if(req.content_type.find("multipart/form-data")==0) {
    const std::string boundary=header_param(req.content_type,"boundary");
    if(boundary.empty()) throw std::runtime_error("Missing multipart boundary");
    const std::string delim="--"+boundary;
    size_t pos=req.body.find(delim);
    while(pos!=std::string::npos) {
      pos+=delim.size();
      if(req.body.compare(pos,2,"--")==0) break;
      if(req.body.compare(pos,2,"\r\n")==0) pos+=2;
      const size_t hend=req.body.find("\r\n\r\n",pos); if(hend==std::string::npos) break;
      const std::string headers=req.body.substr(pos,hend-pos);
      const size_t next=req.body.find("\r\n"+delim,hend+4); if(next==std::string::npos) break;
      const std::string data=req.body.substr(hend+4,next-(hend+4));
      std::string disposition;
      std::istringstream hs(headers); std::string hl;
      while(std::getline(hs,hl)) if(lower(hl).find("content-disposition:")==0) disposition=trim(hl.substr(20));
      const std::string name=header_param(disposition,"name");
      const std::string filename=header_param(disposition,"filename");
      if(!name.empty()) {
        if(disposition.find("filename=")!=std::string::npos) req.files[name]={filename,data};
        else req.fields[name]=data;
      }
      pos=next+2;
    }
  } else if(req.content_type.find("application/x-www-form-urlencoded")==0) {
    std::istringstream items(req.body); std::string item;
    while(std::getline(items,item,'&')) {
      const auto eq=item.find('=');
      req.fields[url_decode(item.substr(0,eq))]=eq==std::string::npos?"":url_decode(item.substr(eq+1));
    }
  }
}

PyObject* make_dict(const std::map<std::string,std::string>& fields) {
  PyObject* d=PyDict_New();
  for(const auto& [key,value]:fields) {
    PyObject* v=PyUnicode_DecodeUTF8(value.data(),value.size(),"replace");
    PyDict_SetItemString(d,key.c_str(),v); Py_DECREF(v);
  }
  return d;
}
PyObject* make_upload_dict(const Request& req,const fs::path& dir) {
  PyObject* d=PyDict_New(); size_t index=0;
  for(const auto& [key,part]:req.files) {
    if(part.filename.empty()) continue;
    std::string name=fs::path(part.filename).filename().string();
    for(char& c:name) if(c==' ' || c=='/' || c=='\\') c='_';
    fs::path path=dir/(std::to_string(index++)+"_"+name);
    std::ofstream out(path,std::ios::binary); out.write(part.data.data(),part.data.size()); out.close();
    PyObject* item=PyDict_New();
    PyObject* filename=PyUnicode_FromStringAndSize(part.filename.data(),part.filename.size());
    const std::string path_str=path.u8string(); PyObject* p=PyUnicode_FromString(path_str.c_str());
    PyDict_SetItemString(item,"filename",filename); PyDict_SetItemString(item,"path",p);
    PyDict_SetItemString(d,key.c_str(),item); Py_DECREF(filename); Py_DECREF(p); Py_DECREF(item);
  }
  return d;
}
std::string py_error() {
  if(!PyErr_Occurred()) return "Python simulation error";
  PyObject *type=nullptr,*value=nullptr,*trace=nullptr;
  PyErr_Fetch(&type,&value,&trace); PyErr_NormalizeException(&type,&value,&trace);
  PyObject* text=value?PyObject_Str(value):nullptr;
  const char* utf=text?PyUnicode_AsUTF8(text):nullptr;
  std::string result=utf?utf:"Python simulation error";
  Py_XDECREF(text); Py_XDECREF(type); Py_XDECREF(value); Py_XDECREF(trace);
  return result;
}
PyObject* py_call(const char* name,PyObject* args) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  PyObject* fn=PyObject_GetAttrString(engine_module,name);
  PyObject* result=fn?PyObject_CallObject(fn,args):nullptr;
  Py_XDECREF(fn); Py_XDECREF(args);
  if(!result && PyErr_Occurred()) PyErr_Print();
  PyGILState_Release(gil);
  return result;
}
std::string py_text(PyObject* value) {
  if(!value) return {};
  PyObject* str=PyObject_Str(value); if(!str) { PyErr_Clear(); return {}; }
  const char* v=PyUnicode_AsUTF8(str); std::string out=v?v:""; Py_DECREF(str); return out;
}
bool py_bool(PyObject* dict,const char* key) { return PyDict_GetItemString(dict,key)!=nullptr; }
long py_long(PyObject* dict,const char* key,long fallback=0) {
  PyObject* value=PyDict_GetItemString(dict,key); if(!value) return fallback;
  long n=PyLong_AsLong(value); if(PyErr_Occurred()) { PyErr_Clear(); return fallback; } return n;
}
double py_double(PyObject* dict,const char* key,double fallback=0) {
  PyObject* value=PyDict_GetItemString(dict,key); if(!value) return fallback;
  double n=PyFloat_AsDouble(value); if(PyErr_Occurred()) { PyErr_Clear(); return fallback; } return n;
}
std::string bytes_string(PyObject* value) {
  if(!value || !PyBytes_Check(value)) return {};
  return std::string(PyBytes_AS_STRING(value),static_cast<size_t>(PyBytes_GET_SIZE(value)));
}
PyObject* invoke_with_payload(const char* fn_name,const std::shared_ptr<Job>& job,
                             const std::string* string_arg=nullptr,const long* int_arg=nullptr) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  PyObject* payload=nullptr;
  { std::lock_guard<std::mutex> jobs_lock(jobs_mutex); payload=job->payload; Py_XINCREF(payload); }
  PyObject* fn=PyObject_GetAttrString(engine_module,fn_name);
  PyObject* extra=string_arg?PyUnicode_FromStringAndSize(string_arg->data(),string_arg->size()):
                  int_arg?PyLong_FromLong(*int_arg):nullptr;
  PyObject* args=PyTuple_New(extra?2:1);
  if(args) {
    PyTuple_SET_ITEM(args,0,payload?payload:Py_None); if(!payload) Py_INCREF(Py_None);
    if(extra) PyTuple_SET_ITEM(args,1,extra);
  } else {
    Py_XDECREF(payload); Py_XDECREF(extra);
  }
  PyObject* result=fn&&args?PyObject_CallObject(fn,args):nullptr;
  if(!result && PyErr_Occurred()) PyErr_Print();
  Py_XDECREF(fn); Py_XDECREF(args);
  PyGILState_Release(gil);
  return result;
}
std::string consume_python_bytes(PyObject* value) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  std::string result=bytes_string(value); Py_XDECREF(value);
  PyGILState_Release(gil); return result;
}
std::string consume_python_text(PyObject* value) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  std::string result=py_text(value); Py_XDECREF(value);
  PyGILState_Release(gil); return result;
}

void update_progress(Job* job,double value,const std::string& message) {
  std::lock_guard<std::mutex> lock(jobs_mutex);
  job->progress=std::clamp(value,0.0,1.0); job->message=message;
}
PyObject* progress_cb(PyObject* self,PyObject* args) {
  double progress; const char* message;
  if(!PyArg_ParseTuple(args,"ds",&progress,&message)) return nullptr;
  auto* job=static_cast<Job*>(PyCapsule_GetPointer(self,"ctsim.job"));
  if(!job) return nullptr;
  update_progress(job,progress,message); Py_RETURN_NONE;
}
PyMethodDef progress_method={"progress",progress_cb,METH_VARARGS,"Report simulation progress to the C++ job manager."};

void run_job(const std::shared_ptr<Job>& job) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  PyObject* fn=PyObject_GetAttrString(engine_module,"execute");
  PyObject* capsule=PyCapsule_New(job.get(),"ctsim.job",nullptr);
  PyObject* callback=PyCFunction_NewEx(&progress_method,capsule,nullptr);
  PyObject* args=PyTuple_Pack(2,job->prepared,callback);
  PyObject* result=fn?PyObject_CallObject(fn,args):nullptr;
  std::string error;
  if(!result) error=py_error();
  {
    std::lock_guard<std::mutex> jobs_lock(jobs_mutex);
    if(result && PyDict_Check(result)) {
      job->payload=result; Py_INCREF(result);
      job->status="done"; job->progress=1.0;
      job->message=py_text(PyDict_GetItemString(result,"message"));
      job->n=static_cast<int>(py_long(result,"n"));
      job->total_deg=py_double(result,"total_deg");
      job->has_zip=py_bool(result,"zip"); job->has_sino_zip=py_bool(result,"sino_zip");
      job->has_batch_zip=py_bool(result,"batch_zip");
      job->batch_dir=py_text(PyDict_GetItemString(result,"batch_dir"));
      PyObject* images=PyDict_GetItemString(result,"images");
      if(images && PyDict_Check(images)) {
        PyObject *key,*value; Py_ssize_t pos=0;
        while(PyDict_Next(images,&pos,&key,&value)) job->kinds.push_back(py_text(key));
        std::sort(job->kinds.begin(),job->kinds.end());
      }
    } else { job->status="error"; job->error=error.empty()?"Python engine returned an invalid result":error; }
    Py_XDECREF(job->prepared); job->prepared=nullptr;
  }
  Py_XDECREF(result); Py_XDECREF(args); Py_XDECREF(callback); Py_XDECREF(capsule); Py_XDECREF(fn);
  PyGILState_Release(gil);
  std::error_code ec; fs::remove_all(job->upload_dir,ec);
}

void prune_jobs() {
  std::vector<std::shared_ptr<Job>> finished;
  {
    std::lock_guard<std::mutex> lock(jobs_mutex);
    for(const auto& [id,job]:jobs) if(job->status!="running") finished.push_back(job);
  }
  std::sort(finished.begin(),finished.end(),[](const auto& a,const auto& b){return a->created<b->created;});
  if(finished.size()<=kMaxJobsKept) return;
  const size_t remove_count=finished.size()-kMaxJobsKept;
  {
    std::lock_guard<std::mutex> lock(jobs_mutex);
    for(size_t i=0;i<remove_count;++i) jobs.erase(finished[i]->id);
  }
  std::lock_guard<std::mutex> py_lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  for(size_t i=0;i<remove_count;++i) {
    Py_XDECREF(finished[i]->prepared); finished[i]->prepared=nullptr;
    Py_XDECREF(finished[i]->payload); finished[i]->payload=nullptr;
  }
  PyGILState_Release(gil);
  for(size_t i=0;i<remove_count;++i) if(!finished[i]->batch_dir.empty()) {
    std::error_code ec; fs::remove_all(finished[i]->batch_dir,ec);
  }
}

Response json_response(std::string body,int status=200) { return {status,"application/json; charset=utf-8",std::move(body),{}}; }
Response text_response(std::string body,int status=200) { return {status,"text/plain; charset=utf-8",std::move(body),{}}; }
Response binary_response(std::string body,std::string type,std::string disposition={}) { return {200,std::move(type),std::move(body),std::move(disposition)}; }
std::string random_id() {
  std::ostringstream out; out<<std::hex<<std::random_device{}()<<id_counter.fetch_add(1);
  std::string id=out.str(); return id.substr(0,std::min<size_t>(10,id.size()));
}
std::shared_ptr<Job> find_job(const std::string& id) {
  std::lock_guard<std::mutex> lock(jobs_mutex); auto it=jobs.find(id);
  return it==jobs.end()?nullptr:it->second;
}
PyObject* prepare_job(const std::string& mode,const Request& req,const fs::path& upload_dir,std::string& error) {
  std::lock_guard<std::mutex> py_lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  PyObject* fn=PyObject_GetAttrString(engine_module,"prepare");
  PyObject* py_mode=PyUnicode_FromString(mode.c_str());
  PyObject* fields=make_dict(req.fields); PyObject* uploads=make_upload_dict(req,upload_dir);
  PyObject* args=PyTuple_Pack(3,py_mode,fields,uploads);
  PyObject* prepared=fn?PyObject_CallObject(fn,args):nullptr;
  if(!prepared) error=py_error();
  Py_XDECREF(args); Py_XDECREF(py_mode); Py_XDECREF(fields); Py_XDECREF(uploads); Py_XDECREF(fn);
  PyGILState_Release(gil);
  return prepared;
}
std::string result_artifact(const std::shared_ptr<Job>& job,const char* key) {
  std::lock_guard<std::mutex> lock(python_mutex);
  PyGILState_STATE gil=PyGILState_Ensure();
  PyObject* payload=nullptr;
  { std::lock_guard<std::mutex> jobs_lock(jobs_mutex); payload=job->payload; Py_XINCREF(payload); }
  std::string path;
  if(payload && PyDict_Check(payload)) path=py_text(PyDict_GetItemString(payload,key));
  Py_XDECREF(payload); PyGILState_Release(gil); return path;
}
std::string render_json_rows(const std::shared_ptr<Job>& job) {
  PyObject* result=invoke_with_payload("batch_rows_json",job);
  return consume_python_text(result);
}
std::string python_image(const std::shared_ptr<Job>& job,const char* fn_name,int index=-1,const std::string& kind={}) {
  const long long_index=index;
  const long* int_arg=index>=0?&long_index:nullptr;
  const std::string* string_arg=!kind.empty()?&kind:nullptr;
  PyObject* result=invoke_with_payload(fn_name,job,string_arg,int_arg);
  return consume_python_bytes(result);
}

Response dispatch(Request req) {
  prune_jobs();
  try { parse_body(req); } catch(const std::exception& e) { return json_response(json_error(e.what()),400); }
  const auto q=req.target.find('?'); const std::string path=req.target.substr(0,q);
  if((req.method=="GET" || req.method=="HEAD") && path=="/") {
    std::string body=replace_all(page_html,"__SLOTS__",page_slots());
    if(req.method=="HEAD") body.clear();
    return {200,"text/html; charset=utf-8",std::move(body),{}};
  }
  if(req.method=="POST" && path=="/api/run") {
    {
      std::lock_guard<std::mutex> lock(jobs_mutex);
      for(const auto& entry:jobs) if(entry.second->status=="running")
        return json_response(json_error("A simulation is already running. Please wait."),409);
    }
    const auto mode_it=req.fields.find("mode");
    const std::string mode=mode_it==req.fields.end()?"scan":mode_it->second;
    const std::string id=random_id();
    fs::path upload_dir=fs::temp_directory_path()/("ct-upload-"+id);
    fs::create_directories(upload_dir);
    std::string error;
    PyObject* prepared=prepare_job(mode,req,upload_dir,error);
    if(!prepared) { std::error_code ec; fs::remove_all(upload_dir,ec); return json_response(json_error(error),400); }
    auto job=std::make_shared<Job>(); job->id=id; job->kind=mode; job->prepared=prepared; job->upload_dir=upload_dir;
    bool busy=false;
    {
      std::lock_guard<std::mutex> lock(jobs_mutex);
      for(const auto& entry:jobs) if(entry.second->status=="running") {
        busy=true; break;
      }
      if(!busy) jobs[id]=job;
    }
    if(busy) {
      std::lock_guard<std::mutex> py_lock(python_mutex); PyGILState_STATE gil=PyGILState_Ensure(); Py_DECREF(prepared); PyGILState_Release(gil);
      std::error_code ec; fs::remove_all(upload_dir,ec);
      return json_response(json_error("A simulation is already running. Please wait."),409);
    }
    std::thread(run_job,job).detach();
    return json_response("{\"job\":\""+json_escape(id)+"\"}");
  }
  std::smatch m;
  auto match=[&](const std::string& pattern){ return std::regex_match(path,m,std::regex(pattern)); };
  if(req.method=="GET" && match("/api/status/([^/]+)")) {
    auto job=find_job(m[1]); if(!job) return json_response("{}",404);
    std::lock_guard<std::mutex> lock(jobs_mutex);
    std::ostringstream out; out<<"{\"status\":\""<<json_escape(job->status)<<"\",\"progress\":"<<job->progress
      <<",\"message\":\""<<json_escape(job->message)<<"\",\"error\":";
    if(job->error.empty()) out<<"null"; else out<<"\""<<json_escape(job->error)<<"\"";
    out<<",\"n\":"<<job->n<<",\"total_deg\":"<<job->total_deg<<",\"has_zip\":"<<(job->has_zip?"true":"false")
      <<",\"has_sino_zip\":"<<(job->has_sino_zip?"true":"false")<<",\"kind\":\""<<json_escape(job->kind)
      <<"\",\"has_batch_zip\":"<<(job->has_batch_zip?"true":"false")<<",\"kinds\":[";
    for(size_t i=0;i<job->kinds.size();++i) { if(i) out<<','; out<<'"'<<json_escape(job->kinds[i])<<'"'; }
    out<<"]}"; return json_response(out.str());
  }
  if(req.method=="GET" && match("/api/image/([^/]+)/([^/]+)\\.png")) {
    auto job=find_job(m[1]); if(!job) return text_response("Not found",404);
    std::string body=python_image(job,"image_bytes",-1,m[2]);
    return body.empty()?text_response("Not found",404):binary_response(std::move(body),"image/png");
  }
  if(req.method=="GET" && match("/api/projection/([^/]+)/(\\d+)\\.png")) {
    auto job=find_job(m[1]); if(!job) return text_response("Not found",404);
    std::string body=python_image(job,"projection_bytes",std::stoi(m[2]));
    return body.empty()?text_response("Not found",404):binary_response(std::move(body),"image/png");
  }
  if(req.method=="GET" && match("/api/batch/([^/]+)")) {
    auto job=find_job(m[1]); if(!job) return json_response("{}",404);
    return json_response(render_json_rows(job));
  }
  if(req.method=="GET" && match("/api/thumb/([^/]+)/(\\d+)\\.png")) {
    auto job=find_job(m[1]); if(!job) return text_response("Not found",404);
    std::string body=python_image(job,"thumb_bytes",std::stoi(m[2]));
    return body.empty()?text_response("Not found",404):binary_response(std::move(body),"image/png");
  }
  struct Download { const char* route; const char* key; const char* filename; };
  const Download downloads[]={{"/api/download/([^/]+)","zip","ct_dataset.zip"},
    {"/api/download_sinogram/([^/]+)","sino_zip","sinogram_dataset.zip"},
    {"/api/download_batch/([^/]+)","batch_zip","datasets.zip"},
    {"/api/manifest/([^/]+)","manifest","manifest.csv"}};
  if(req.method=="GET") for(const auto& d:downloads) if(match(d.route)) {
    auto job=find_job(m[1]); if(!job) return text_response("Not found",404);
    std::string file=result_artifact(job,d.key); if(file.empty()) return text_response("Not found",404);
    const std::string filename=d.filename;
    std::string mime=filename.size()>=4 && filename.substr(filename.size()-4)==".csv"?"text/csv":"application/zip";
    Response response=binary_response({},mime,std::string("attachment; filename=\"")+d.filename+"\"");
    response.file_path=file;
    return response;
  }
  return json_response("{}",404);
}

void send_response(int fd,const Response& response) {
  const char* reason=response.status==200?"OK":response.status==400?"Bad Request":response.status==404?"Not Found":
      response.status==409?"Conflict":response.status==413?"Payload Too Large":"Internal Server Error";
  uintmax_t size=response.body.size();
  if(!response.file_path.empty()) {
    std::error_code ec; size=fs::file_size(response.file_path,ec);
    if(ec) size=0;
  }
  std::ostringstream head; head<<"HTTP/1.1 "<<response.status<<' '<<reason<<"\r\n"
    <<"Content-Type: "<<response.type<<"\r\nContent-Length: "<<size<<"\r\nConnection: close\r\n";
  if(!response.disposition.empty()) head<<"Content-Disposition: "<<response.disposition<<"\r\n";
  head<<"X-Content-Type-Options: nosniff\r\n\r\n";
  const std::string h=head.str(); send(fd,h.data(),h.size(),0);
  if(!response.file_path.empty()) {
    std::ifstream file(response.file_path,std::ios::binary); char buffer[65536];
    while(file) {
      file.read(buffer,sizeof(buffer)); const auto got=file.gcount(); if(got<=0) break;
      size_t sent=0; while(sent<static_cast<size_t>(got)) {
        const ssize_t n=send(fd,buffer+sent,static_cast<size_t>(got)-sent,0);
        if(n<=0) return; sent+=static_cast<size_t>(n);
      }
    }
  } else {
    size_t sent=0; while(sent<response.body.size()) {
      const ssize_t n=send(fd,response.body.data()+sent,response.body.size()-sent,0);
      if(n<=0) break; sent+=static_cast<size_t>(n);
    }
  }
}
void handle_client(int fd) {
  Request req;
  if(!read_request(fd,req)) send_response(fd,{400,"text/plain; charset=utf-8","Malformed or oversized request",{}});
  else {
    try { send_response(fd,dispatch(std::move(req))); }
    catch(const std::exception& e) { send_response(fd,{500,"text/plain; charset=utf-8",e.what(),{}}); }
  }
  close_socket(fd);
}
} // namespace

int main(int argc,char** argv) {
  const int port=argc>1?std::stoi(argv[1]):5000;
#ifdef _WIN32
  WSADATA wsa;
  if(WSAStartup(MAKEWORD(2,2),&wsa)!=0) { std::cerr<<"WSAStartup failed\n"; return 1; }
#endif
  Py_Initialize();
  PyObject* sys_path=PySys_GetObject("path");
  PyObject* cwd=PyUnicode_FromString("."); PyList_Insert(sys_path,0,cwd); Py_DECREF(cwd);
  if(const char* site=std::getenv("CT_PYTHON_SITE")) { PyObject* p=PyUnicode_FromString(site); PyList_Insert(sys_path,0,p); Py_DECREF(p); }
  engine_module=PyImport_ImportModule("simulator_engine");
  if(!engine_module) { PyErr_Print(); std::cerr<<"Could not import simulator_engine.py\n"; return 1; }
  std::ifstream html_file("index.html",std::ios::binary);
  if(!html_file) { std::cerr<<"Could not open index.html\n"; return 1; }
  page_html.assign(std::istreambuf_iterator<char>(html_file),std::istreambuf_iterator<char>());
  PyEval_SaveThread();

  std::signal(SIGINT,stop_server); std::signal(SIGTERM,stop_server);
#ifdef SIGPIPE
  std::signal(SIGPIPE,SIG_IGN);
#endif
  const int server=static_cast<int>(socket(AF_INET,SOCK_STREAM,0)); if(server<0) { std::perror("socket"); return 1; }
  #ifndef _WIN32
  int reuse=1; setsockopt(server,SOL_SOCKET,SO_REUSEADDR,&reuse,sizeof(reuse));
#endif
  sockaddr_in address{}; address.sin_family=AF_INET; address.sin_addr.s_addr=htonl(INADDR_ANY); address.sin_port=htons(static_cast<uint16_t>(port));
  if(bind(server,reinterpret_cast<sockaddr*>(&address),sizeof(address))<0 || listen(server,32)<0) {
    std::perror("bind/listen"); close_socket(server); return 1;
  }
  std::cout<<"C++ CT simulator server listening on http://127.0.0.1:"<<port<<std::endl;
  while(server_running) {
    sockaddr_in peer{}; socklen_t length=sizeof(peer);
    const int fd=static_cast<int>(accept(server,reinterpret_cast<sockaddr*>(&peer),&length));
    if(fd<0) { if(server_running) std::perror("accept"); continue; }
    std::thread(handle_client,fd).detach();
  }
  close_socket(server); return 0;
}
