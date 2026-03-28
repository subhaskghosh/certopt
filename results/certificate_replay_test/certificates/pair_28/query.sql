SELECT
  p.firstname,
  p.lastname,
  address.city,
  address.state
FROM PERSON AS P
LEFT JOIN ADDRESS
  ON address.personid = p.personid