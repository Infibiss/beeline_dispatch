<?
  // Load heat maps from iprobki.ru
  function file_get_contents2($url) {
    for ($i=0; $i<1; $i++) {
      $agent= 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1; .NET CLR 1.0.3705; .NET CLR 1.1.4322)';
      $ch = curl_init();
      curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
      curl_setopt($ch, CURLOPT_VERBOSE, true);
      curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
      curl_setopt($ch, CURLOPT_USERAGENT, $agent);
      curl_setopt($ch, CURLOPT_URL, $url);
      curl_setopt($ch, CURLOPT_TIMEOUT, 5);
      $return = curl_exec($ch);
      //print_r($return);
      if ($return === false) {
        echo "Error: ".curl_error($ch)."<br>";
      } else break;
    }
    //print_r(curl_getinfo($ch));
    curl_close($ch);
    return $return;
  }

  // Analyze iprobki.ru images
  $y2 = "";
  for ($y=2012; $y<2016; $y++) {
    if ($y > 2012) $y2 = $y;
    for ($m=1; $m<13; $m++) {
      $m2 = str_pad($m, 2, "0", STR_PAD_LEFT);
      $url = "http://iprobki.ru/public/img/heatmap/$y2$m2.png";
      echo "$url<br>";
      $fname = "cache3/$y$m2.png";
      file_put_contents($fname, file_get_contents2($url));
      //$tileImg = imagecreatefrompng($fname);
      //imagedestroy($tileImg);
    }
  }
?>