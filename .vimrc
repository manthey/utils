" see :options
" Long json can throw an error; increase pattern matching memory
set maxmempattern=50000
" expandtabs
set et
set tabstop=4
" shiftwidth
set sw=4
set nocindent
" autoindent
set ai
" tell indenting programs that we already indented the buffer
let b:did_indent = 1
" don't do an incremental search (don't search before we finish typing)
set nois
" don't ignore case by default
set noic
" don't break at 80 characters
set wrap
" don't add linebreaks at 80 characters
set nolbr
" highlight all search matches
set hls
" The autodetect doesn't work very well. 
set background=dark
" make Spell turn on spell checking
command Spell set spell spelllang=en_us
hi clear SpellBad
hi SpellBad ctermfg=7 ctermbg=1
" hi clear SpellRare
" hi SpellRare cterm=underline
" hi clear SpellCap
" hi SpellCap cterm=underline
hi clear SpellLocal
hi SpellLocal cterm=underline
" Set some file types based on extension
autocmd BufNewFile,BufRead *.json set filetype=json
autocmd BufNewFile,BufRead *.pac set filetype=javascript
autocmd BufNewFile,BufRead *.styl set filetype=stylus
" turn on spell checking for some file extensions
autocmd BufNewFile,BufRead *.md setlocal spell spelllang=en_us

" disable trying to connect to an X server
set clipboard=exclude:.*

" type zg when over a 'misspelled' word to add it to the spellfile dictionary
"      z= to show spelling suggestions.
" default to utf-8
set enc=utf-8
" show the cursor position
set ruler
" allow backspace to go to the previous line
set bs=2
" keep this much history
set history=50
" don't try to maintain vi compatibility
set nocompatible

" adjust colors so in 256color mode I can still see things
" type "highlight" to see what the current settings are
hi Search term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi clear Visual
hi Visual term=reverse cterm=reverse guibg=LightGrey
hi DiffAdd term=bold ctermbg=4 guibg=LightBlue
hi DiffChange term=bold ctermbg=5 guibg=LightMagenta
hi DiffText term=reverse cterm=bold ctermbg=1 gui=bold guibg=Red
hi SpellBad ctermfg=7 ctermbg=1
hi SpellCap term=reverse ctermbg=4 gui=undercurl guisp=Blue
hi SpellRare term=reverse ctermbg=5 gui=undercurl guisp=Magenta
hi SpellLocal cterm=underline
hi clear CursorColumn
hi CursorColumn term=reverse cterm=reverse gui=reverse
hi ColorColumn term=reverse ctermbg=1 guibg=LightRed
hi QuickFixLine term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi MatchParen term=reverse ctermfg=0 ctermbg=3 guibg=Yellow
hi ToolbarLine term=underline ctermfg=0 ctermbg=3 guibg=Yellow
hi SignColumn term=standout ctermfg=4 ctermbg=7 guifg=DarkBlue guibg=Grey

" We use to need pathogen, but modern vim doesn't.  In modern vim, git clone
" plugins in ~/.vim/pack/vendor/start (%USERPROFILE%\vimfiles\pack\plugins\start
" on Windows).  Git clone these (and more below) in that directory:
"  https://github.com/scrooloose/syntastic
"  https://github.com/Stormherz/tablify
"  https://github.com/leafgarland/typescript-vim
"  https://github.com/tikhomirov/vim-glsl
"  https://github.com/pangloss/vim-javascript
"  https://github.com/gabrielelana/vim-markdown
"  https://github.com/digitaltoad/vim-pug
"  https://github.com/posva/vim-vue
"  https://github.com/Quramy/vison

" syntastic block start
if 1
"syntastic requires pathogen (see https://github.com/scrooloose/syntastic)
silent! call pathogen#infect()

let g:syntastic_always_populate_loc_list = 1
let g:syntastic_auto_loc_list = 1
let g:syntastic_check_on_open = 0
let g:syntastic_check_on_wq = 0
let g:syntastic_javascript_checkers=['eslint']
let g:syntastic_pug_checkers=['pug_list']
"let g:syntastic_javascript_eslint_exe = '$(npm bin)/eslint'
let g:syntastic_stylus_checkers=['stylint']
" let g:syntastic_python_python_exec = 'python3.7'
" let g:syntastic_python_flake8_exe = 'python3.7 -m flake8'
let g:syntastic_python_flake8_args = '--format=''%(path)s:%(row)d:%(col)d: %(code)s %(text)s'''

let g:SuperTabNoCompleteAfter=['^', '\s', '\*', '//']
endif
" syntastic block end


" remove trailing whitespace
function! s:StripTrailingWhitespaces()
    let l = line(".")
    let c = col(".")
    try
        silent undojoin
    catch
    endtry
    %s/\s\+$//e
    call cursor(l, c)
endfunction

" remove empty lines at the end of a file
function! s:TrimEndLines()
    let s:l = line(".")
    let s:c = col(".")
    try
        silent undojoin
    catch
    endtry
    %s#\($\n\s*\)*\%$##
    call cursor(s:l, s:c)
endfunction

function! s:AddEndLine()
    let l = line(".")
    let c = col(".")
    let m = &modified
    try
        silent undojoin
    catch
    endtry
    $s#$#\r#
    let &modified = m
    call cursor(l, c)
endfunction

function! s:AddEndLineAfterWrite()
    call s:AddEndLine()
    call cursor(s:l, s:c)
endfunction

autocmd! BufNewFile,BufRead *.vs,*.fs set ft=glsl
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePre <buffer> :call s:StripTrailingWhitespaces()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePre <buffer> :call s:TrimEndLines()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufEnter <buffer> :call s:AddEndLine()
autocmd FileType c,cpp,javascript,jade,php,ruby,python,stylus,pug,cmake,yaml,tmpl,dockerfile,vue,glsl,dosini,typescript autocmd BufWritePost <buffer> :call s:AddEndLineAfterWrite()

function LargeFile()
 " no syntax highlighting etc
 set eventignore+=FileType
 " save memory when other file is viewed
 setlocal bufhidden=unload
 " no undo possible
 " setlocal undolevels=-1
 " disable syntastic
 let g:syntastic_mode_map = { 'mode': 'passive', 'active_filetypes': [],'passive_filetypes': [] }
 " display message
 " autocmd VimEnter *  echo "The file is larger than " . (g:LargeFile / 1024 / 1024) . " MB, so some options are changed (see .vimrc for details)."
endfunction

let g:LargeFile = 1024 * 1024 * 2
augroup  LargeFile
    autocmd BufReadPre * let f=getfsize(expand("<afile>")) | if f > g:LargeFile || f == -2 | call LargeFile() | endif
augroup END

" backup to a single hidden directory with date-stamped backups.  Keep a
" maximum of 2500 files in the backup directory
set backupdir=$HOME/.vim_backup
if strlen(finddir(&g:backupdir))==0
    call mkdir(&g:backupdir, "p", 0770)
endif
if has("win32")
    call system("for /f \"tokens=* skip=2500\" \%F in ('dir ".shellescape(&g:backupdir)." /o-d /tc /b') do del ".shellescape(&g:backupdir."\\\%F"))
else
    call system("find ".shellescape(&g:backupdir)." -type f -print0 | xargs -0 ls -A1tr | head -n -2500 | xargs -d '\n' rm -f")
endif
execute "set backupext=_".strftime("%y%m%d%H%M")
set nobackup

function! s:save_copy(filename, ismod)
    " File must exist and be modified since creation
    let filename = fnamemodify(a:filename, ":p")
    if !filereadable(filename)
        return
    endif
    if (!a:ismod)
        return
    endif
    " Don't backup files that are more than 10 Mb
    if (getfsize(filename) > 10000000)
        return
    endif
    let backup=fnamemodify(&backupdir, ":p").fnamemodify(filename, ":t")."_".strftime("%y%m%d%H%M%S", getftime(filename))
    if has("win32")
        let cmd = "copy /y ".shellescape(filename)." ".shellescape(backup)
    else
        let cmd = "cp ".fnameescape(filename)." ".fnameescape(backup)
    endif
    let result = system(cmd)
endfunction

autocmd BufWritePre * let ismod=&mod
autocmd BufWritePre,BufWritePost * call s:save_copy(expand('<afile>'), ismod)
" end of backup settings

" syntax highlighting is on
syntax on
set synmaxcol=1000
" Sometime syntax doesn't search far enough to format properly.  'fromstart' is
" slow, but will work.  'minlines=..' is less slow.  'clear' resets to the
" defaults.  This doesn't stick for many file types
" syntax sync fromstart
" syntax sync minlines=200
autocmd FileType vue syntax sync fromstart
" don't do syntax highlight on big files
autocmd BufReadPre * if getfsize(expand("%")) > 10000000 | syntax clear | endif

" save information for 100 files, with up to 50 lines for each register
set viminfo='100,\"50
if v:lang =~ "utf8$" || v:lang =~ "UTF-8$"
    set fileencodings=utf-8,latin1
endif
if has("autocmd")
    " When editing a file, always jump to the last cursor position
    autocmd BufReadPost *
    \ if line("'\"") > 0 && line ("'\"") <= line("$") |
    \   exe "normal! g'\"" |
    \ endif
endif
autocmd FileType c set nocindent

" Insert Mode -> normal cursor (line)
let &t_SI .= "\e[5 q"
" " Normal Mode -> block cursor
let &t_EI .= "\e[1 q"

" fix gitcommit
autocmd FileType gitcommit setlocal spell spelllang=en_us
" On git commits, reflow paragraphs and set the text width to 72.
autocmd FileType gitcommit set tw=72
" autocmd FileType gitcommit set formatoptions+=at
autocmd FileType gitcommit set formatoptions=cqat
" But this looks bad in github PRs, so stop doing it.
" autocmd FileType gitcommit set formatoptions-=at

" Don't enable mouse editing.  It does surprising things in windows
set mouse=
set ttymouse=

" vim packages:
"  https://github.com/prabirshrestha/vim-lsp
"  https://github.com/prabirshrestha/asyncomplete-lsp.vim
"  https://github.com/prabirshrestha/asyncomplete.vim
" pip install python-lsp-server[all]
" npm install -g bash-language-server
" npm install -g typescript typescript-language-server
if executable('pylsp')
  au User lsp_setup call lsp#register_server({
        \ 'name': 'pylsp',
        \ 'cmd': {server_info->['pylsp']},
        \ 'whitelist': ['python'],
        \ })
endif
if executable('/home/manthey/.nvm/versions/node/v22.13.1/bin/typescript-language-server')
  au User lsp_setup call lsp#register_server({
        \ 'name': 'tsserver',
        \ 'cmd': {server_info->[
        \   '/home/manthey/.nvm/versions/node/v22.13.1/bin/node',
        \   '/home/manthey/.nvm/versions/node/v22.13.1/bin/typescript-language-server',
        \   '--stdio'
        \ ]},
        \ 'whitelist': ['javascript', 'typescript'],
        \ })
endif

if executable('/home/manthey/.nvm/versions/node/v22.13.1/bin/bash-language-server')
  au User lsp_setup call lsp#register_server({
        \ 'name': 'bashls',
        \ 'cmd': {server_info->[
        \   '/home/manthey/.nvm/versions/node/v22.13.1/bin/node',
        \   '/home/manthey/.nvm/versions/node/v22.13.1/bin/bash-language-server',
        \   'start'
        \ ]},
        \ 'whitelist': ['sh', 'bash'],
        \ })
endif
let g:lsp_diagnostics_enabled = 0
let g:lsp_diagnostics_virtual_text_enabled = 0
let g:lsp_diagnostics_signs_enabled = 0
let g:lsp_diagnostics_echo_cursor = 0
let g:lsp_document_code_action_signs_enabled = 0

" Enable asyncomplete integration with vim-lsp
let g:asyncomplete_auto_popup = 1
let g:asyncomplete_auto_completeopt = 0

" Ensure Tab cycles the popup
inoremap <expr> <Tab> pumvisible() ? "\<C-n>" : "\<Tab>"
inoremap <expr> <S-Tab> pumvisible() ? "\<C-p>" : "\<S-Tab>"
inoremap <expr> <CR> pumvisible() ? (complete_info().selected != -1 ? "\<C-y>" : "\<C-e>\<CR>") : "\<CR>"
inoremap <expr> <Down>  pumvisible() && complete_info().selected != -1 ? "\<C-n>" : "\<Down>"
inoremap <expr> <Up>    pumvisible() && complete_info().selected != -1 ? "\<C-p>" : "\<Up>"
inoremap <expr> <Left>  pumvisible() && complete_info().selected != -1 ? "\<C-e>\<Left>" : "\<Left>"
inoremap <expr> <Right> pumvisible() && complete_info().selected != -1 ? "\<C-e>\<Right>" : "\<Right>"
highlight Pmenu      ctermfg=LightGray ctermbg=DarkBlue 
highlight PmenuSel   ctermfg=Black ctermbg=Yellow
highlight PmenuSbar  ctermfg=NONE  ctermbg=DarkBlue
highlight PmenuThumb ctermfg=NONE  ctermbg=White
highlight link markdownCodeDelimiter Normal
highlight link markdownBoldDelimiter Normal
highlight link markdownItalicDelimiter Normal
highlight link markdownHeadingDelimiter Normal

set completeopt=menuone,noinsert,noselect
set shortmess+=c

